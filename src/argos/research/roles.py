"""Stateless RM and independent RA roles, using the same structured-call interface."""

import asyncio
import json
from typing import Literal

from pydantic import Field

from argos.backends import LLMRequest
from argos.common import EntityId, EntityReference, Model, Text
from argos.protocols import (
    DispatchResearchAgents,
    ManagerAction,
    ProposeExperiment,
    ResearchAgentResult,
    ResearchTask,
)
from argos.state.store import StateSnapshot

from .prompts import FALSIFICATION_PROMPT, MANAGER_PROMPT, RESEARCH_AGENT_PROMPT
from .runtime import AgentOutcome, StructuredCaller


class ManagerPlan(Model):
    summary: Text
    actions: list[ManagerAction] = Field(min_length=1, max_length=20)


class ResearchBatch(Model):
    outcomes: list[AgentOutcome[ResearchAgentResult]]

    @property
    def completed_results(self) -> list[ResearchAgentResult]:
        return [o.output for o in self.outcomes if o.output is not None]


class ResearchManager:
    """Called by a host on completed events; no polling, store or execution tools."""

    def __init__(self, caller: StructuredCaller):
        self.caller = caller

    async def plan(
        self,
        snapshot: StateSnapshot,
        *,
        task_id: EntityId,
        event: Literal[
            "initialized",
            "observations_recorded",
            "review_recorded",
            "hypotheses_recorded",
            "human_answered",
        ],
    ) -> AgentOutcome[ManagerPlan]:
        if event not in {
            "initialized",
            "observations_recorded",
            "review_recorded",
            "hypotheses_recorded",
            "human_answered",
        }:
            raise ValueError("RM requires a completed research event, not a polling tick")
        return await self._invoke(snapshot, task_id, event, None)

    async def synthesize(
        self,
        snapshot: StateSnapshot,
        batch: ResearchBatch,
        *,
        task_id: EntityId,
    ) -> AgentOutcome[ManagerPlan]:
        # Join first; retain every successful result and explicit failed task, even if empty.
        batch = ResearchBatch.model_validate_json(batch.model_dump_json())
        if any(o.task.project_id != snapshot.project.id for o in batch.outcomes):
            raise ValueError("Synthesis batch belongs to another project")
        ids = [o.task.id for o in batch.outcomes]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate task in synthesis batch")
        return await self._invoke(snapshot, task_id, "exploration_completed", batch)

    async def _invoke(self, snapshot, task_id, event, batch):
        snapshot = StateSnapshot.model_validate_json(snapshot.model_dump_json())
        if not snapshot.project.main_question_approved or not snapshot.main_research_question:
            raise ValueError("Human must approve the main question before research starts")
        if snapshot.project.status != "active":
            raise ValueError("Research project is not active")
        if snapshot.main_research_question != snapshot.project.config.main_research_question:
            raise ValueError("Snapshot question must match the approved project question")
        # Limit rows and omit repeated execution logs / old agent actions. The hard character
        # cap rejects oversized context rather than silently cutting evidence or the charter.
        fields = (
            "active_subproblems",
            "active_branches",
            "hypotheses",
            "recent_observations",
            "candidate_claims",
            "failed_directions",
            "abandoned_branches",
            "rejected_claims",
            "reviews",
        )
        data = snapshot.model_dump(mode="json")
        frontier = {key: data[key][:8] for key in fields}
        frontier["truncated"] = sorted(
            set(snapshot.truncated) | {key for key in fields if len(data[key]) > 8}
        )
        frontier["failed_runs"] = [
            {
                "id": str(run.id),
                "experiment_id": str(run.experiment_id),
                "failure": run.result.failure.model_dump(mode="json"),
            }
            for run in snapshot.failed_runs[:8]
            if run.result and run.result.failure
        ]
        frontier["recent_experiments"] = [
            {
                "id": str(exp.id),
                "hypothesis_id": str(exp.spec.hypothesis_id),
                "status": exp.status,
                "goal": exp.spec.goal,
            }
            for exp in snapshot.recent_experiments[:8]
        ]
        frontier["decisions"] = [
            {
                "id": str(decision.id),
                "summary": decision.summary,
                "rationale": decision.rationale,
                "references": [ref.model_dump(mode="json") for ref in decision.references],
            }
            for decision in snapshot.decisions[:5]
        ]
        for key, limit in (("failed_runs", 8), ("recent_experiments", 8), ("decisions", 5)):
            if len(data[key]) > limit and key not in frontier["truncated"]:
                frontier["truncated"].append(key)
        context = {
            "event": event,
            "project": data["project"],
            "frontier": frontier,
            "exploration": batch.model_dump(mode="json") if batch else None,
        }

        def validate(plan: ManagerPlan):
            for action in plan.actions:
                if action.project_id != snapshot.project.id:
                    raise ValueError("Manager action belongs to another project")
                payload = action.action
                if isinstance(payload, DispatchResearchAgents):
                    for task in payload.tasks:
                        if task.context_policy != "independent":
                            raise ValueError("Initial RA tasks must be independent")
                        if task.main_research_question != snapshot.main_research_question:
                            raise ValueError("RA task must use the canonical main question")
                if isinstance(payload, ProposeExperiment):
                    config = snapshot.project.config
                    if payload.spec.evaluation_protocol != config.evaluation_protocol:
                        raise ValueError("Experiment cannot replace the protected evaluator")
                    if payload.spec.baseline_id != snapshot.project.baseline_id:
                        raise ValueError("Experiment must pin the current baseline")
                    if payload.spec.scope != config.scope:
                        raise ValueError("Experiment must preserve the configured path scope")
                    limits = payload.spec.resource_limits
                    for key, ceiling in config.resource_limits.model_dump().items():
                        requested = getattr(limits, key)
                        if ceiling is not None and (requested is None or requested > ceiling):
                            raise ValueError("Experiment exceeds configured resource limits")

        request = LLMRequest(
            task_id=task_id,
            role="manager",
            system_prompt=MANAGER_PROMPT,
            context_json=json.dumps(context),
            output_schema=ManagerPlan.model_json_schema(),
        )
        return await self.caller.call(request, snapshot.project.id, ManagerPlan, validate=validate)


class ResearchAgent:
    def __init__(self, caller: StructuredCaller):
        self.caller = caller

    async def explore(self, task: ResearchTask) -> AgentOutcome[ResearchAgentResult]:
        task = ResearchTask.model_validate_json(task.model_dump_json())
        agent_id = f"ra:{task.branch_id}"
        prompt = RESEARCH_AGENT_PROMPT
        if task.perspective.casefold() == "falsification":
            prompt += FALSIFICATION_PROMPT
        request = LLMRequest(
            task_id=task.id,
            role="research_agent",
            system_prompt=prompt,
            context_json=json.dumps({"task": task.model_dump(mode="json"), "agent_id": agent_id}),
            output_schema=ResearchAgentResult.model_json_schema(),
        )

        def validate(result: ResearchAgentResult):
            if result.task_id != task.id or result.agent_id != agent_id:
                raise ValueError("Result must match its assigned task and agent")

        return await self.caller.call(
            request,
            task.project_id,
            ResearchAgentResult,
            references=[EntityReference(entity_type="research_branch", entity_id=task.branch_id)],
            validate=validate,
        )


class ResearchDispatcher:
    """One synchronous round: freeze all initial inputs, dispatch, then join.

    Only assigned ResearchTask data enters each request. No project-wide snapshot,
    sibling task or output is appended. The backend must honor stateless requests.
    """

    def __init__(self, agent: ResearchAgent):
        self.agent = agent

    async def dispatch(self, action: ManagerAction) -> ResearchBatch:
        action = ManagerAction.model_validate_json(action.model_dump_json())
        if not isinstance(action.action, DispatchResearchAgents):
            raise ValueError("Expected a dispatch_research_agents action")
        tasks = action.action.tasks
        if any(task.context_policy != "independent" for task in tasks):
            raise ValueError("Initial exploration requires independent contexts")
        # No shared mutable context: all task copies exist before any response is generated.
        copies = [task.model_copy(deep=True) for task in tasks]
        outcomes = await asyncio.gather(*(self.agent.explore(task) for task in copies))
        return ResearchBatch(outcomes=outcomes)
