"""Blind claim review; agents propose reviews, trusted host code persists them."""

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import Field, model_validator

from argos.backends import FakeLLMBackend, LLMRequest
from argos.common import EntityId, EntityReference, Model, Text
from argos.models import Claim, Project, Subproblem
from argos.protocols import CriticReview, EvaluatorResult, ExperimentResult, ExperimentSpec
from argos.state import StateError, StateStore

from .prompts import CRITIC_PROMPT
from .runtime import AgentOutcome, StructuredCaller


class CriticEvidence(Model):
    """One observation and its raw evidence, without hypothesis-generation reasoning."""

    observation_id: EntityId
    relation: Literal["supports", "contradicts"]
    observation_summary: Text
    subproblem: Text
    spec: ExperimentSpec
    result: ExperimentResult
    evaluation: EvaluatorResult
    code_diff: str  # Empty is a legitimate unchanged experiment, not missing evidence.

    @model_validator(mode="after")
    def matching_evidence(self):
        if self.spec.experiment_id != self.result.experiment_id:
            raise ValueError("Evidence experiment identities must match")
        if (self.evaluation.experiment_id, self.evaluation.run_id) != (
            self.result.experiment_id,
            self.result.run_id,
        ):
            raise ValueError("Evaluation must match the reviewed run")
        if self.evaluation.protocol_name != self.spec.evaluation_protocol.name:
            raise ValueError("Evaluation must use the experiment protocol")
        return self


class CriticInput(Model):
    project_id: EntityId
    main_research_question: Text
    held_out_test_protocol: Text
    claim: Claim
    evidence: list[CriticEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def complete_claim_evidence(self):
        if self.claim.project_id != self.project_id:
            raise ValueError("Claim belongs to another project")
        ids = [item.observation_id for item in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate review evidence")
        for relation, expected in (
            ("supports", self.claim.evidence.supporting_observation_ids),
            ("contradicts", self.claim.evidence.contradicting_observation_ids),
        ):
            actual = {e.observation_id for e in self.evidence if e.relation == relation}
            if actual != set(expected):
                raise ValueError("Review must include all supporting and contradicting evidence")
        return self


class Critic(Protocol):
    async def review(
        self, context: CriticInput, *, task_id: EntityId
    ) -> AgentOutcome[CriticReview]: ...


class LLMCritic:
    """A fresh bounded request; no RM transcript, snapshot or synthesis argument."""

    def __init__(self, caller: StructuredCaller):
        self.caller = caller

    async def review(
        self, context: CriticInput, *, task_id: EntityId
    ) -> AgentOutcome[CriticReview]:
        context = CriticInput.model_validate_json(context.model_dump_json())

        def validate(review: CriticReview):
            if review.claim_id != context.claim.id:
                raise ValueError("Review must refer to the assigned claim")

        return await self.caller.call(
            LLMRequest(
                task_id=task_id,
                role="critic",
                system_prompt=CRITIC_PROMPT,
                context_json=context.model_dump_json(),
                output_schema=CriticReview.model_json_schema(),
            ),
            context.project_id,
            CriticReview,
            references=[EntityReference(entity_type="claim", entity_id=context.claim.id)],
            validate=validate,
        )


class FakeCritic(LLMCritic):
    """Scripted verdicts (or invalid JSON/errors), through production validation.

    No simulated scientific judgment: each task consumes its supplied responses.
    Use LLMCritic with a shared StructuredCaller to share resource slots across roles.
    """

    def __init__(
        self,
        responses: Mapping[EntityId, Sequence[CriticReview | str | Exception]],
        **caller_options,
    ):
        self.backend = FakeLLMBackend(
            {
                key: [r.model_dump_json() if isinstance(r, CriticReview) else r for r in values]
                for key, values in responses.items()
            }
        )
        super().__init__(StructuredCaller(self.backend, **caller_options))


class CriticState:
    """Host-only projection and persistence. Never expose this object to a backend."""

    def __init__(self, store: StateStore):
        self.store = store

    def prepare(self, claim_id: EntityId, *, code_diffs: Mapping[EntityId, str]) -> CriticInput:
        """Host supplies diff contents keyed by run ID from trusted artifact storage.

        Do not open model-supplied paths. Include every linked observation, including
        counterevidence; oversized inputs fail at the caller rather than being truncated.
        """
        with self.store.transaction():
            claim = self.store.get(Claim, claim_id)
            project = self.store.get(Project, claim.project_id)
            if not project.main_question_approved or project.status != "active":
                raise StateError("Critic requires an active project with an approved question")
            evidence = []
            for row in self.store.claim_provenance(claim.id):
                if row.run.result is None or row.run.evaluation is None:
                    raise StateError("Review evidence requires recorded execution and evaluation")
                if row.run.id not in code_diffs:
                    raise StateError("Missing code diff for review evidence")
                evidence.append(
                    CriticEvidence(
                        observation_id=row.observation.id,
                        relation=row.evidence.relation,
                        observation_summary=row.observation.summary,
                        subproblem=self.store.get(
                            Subproblem, row.hypothesis.subproblem_id
                        ).question,
                        spec=row.experiment.spec,
                        result=row.run.result,
                        evaluation=row.run.evaluation,
                        code_diff=code_diffs[row.run.id],
                    )
                )
            return CriticInput(
                project_id=project.id,
                main_research_question=project.config.main_research_question,
                held_out_test_protocol=project.config.held_out_test_protocol,
                claim=claim,
                evidence=evidence,
            )

    def record(self, context: CriticInput, outcome: AgentOutcome[CriticReview]) -> EntityId | None:
        """Persist only a valid review of unchanged evidence. Never change claim status.

        The caller's record_task hook persists failures. On success emit review_recorded
        to RM only after this returns; RM consumes reviews through the bounded snapshot.
        """
        context = CriticInput.model_validate_json(context.model_dump_json())
        outcome = AgentOutcome[CriticReview].model_validate_json(outcome.model_dump_json())
        if outcome.task.project_id != context.project_id or outcome.task.kind != "critic":
            raise StateError("Outcome must belong to this project's Critic task")
        expected = [EntityReference(entity_type="claim", entity_id=context.claim.id)]
        if outcome.task.references != expected:
            raise StateError("Critic task must reference the reviewed claim")
        if outcome.output is None:
            return None
        if outcome.output.claim_id != context.claim.id:
            raise StateError("Review must refer to the assigned claim")
        with self.store.transaction():
            current = self.prepare(
                context.claim.id,
                code_diffs={e.result.run_id: e.code_diff for e in context.evidence},
            )
            if current != context:
                raise StateError("Claim or evidence changed since review input was prepared")
            return self.store.record_review(outcome.output, reviewed_claim=context.claim)
