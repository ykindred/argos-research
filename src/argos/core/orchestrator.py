"""Synchronous, event-driven research cycles with atomic SQLite checkpoints.

No scientific decisions live here. External calls are journaled before dispatch;
completed outputs and state transitions commit together. Lost calls are failed,
never silently replayed. A project has one local runner (enforced by the CLI lock).
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from argos import models as m
from argos.common import ExecutionFailure
from argos.evaluation import record_evaluation
from argos.protocols import EvaluatorResult, ExperimentResult, ResearchAgentResult
from argos.research import (
    AgentOutcome,
    CriticState,
    LLMCritic,
    ManagerPlan,
    ResearchAgent,
    ResearchBatch,
    ResearchDispatcher,
    ResearchManager,
    ResearchStateWriter,
    StructuredCaller,
    TaskRecordingError,
)
from argos.research.briefing import BriefingBuilder
from argos.state import StateError, StateStore

from .recovery import interrupted_result
from .state import HumanGate, Operation, RuntimeState


def now():
    return datetime.now(UTC)


class Orchestrator:
    def __init__(self, store: StateStore, project_id: UUID, backend, executor, evaluator):
        self.store, self.project_id = store, project_id
        self.executor, self.evaluator = executor, evaluator
        self.writer = ResearchStateWriter(store)
        config = self.project.config
        caller = StructuredCaller(
            backend,
            llm_slots=config.resources.llm_slots,
            timeout_seconds=config.resource_limits.timeout_seconds,
            record_task=self.writer.record_task,
            record_outcome=self._record_outcome,
        )
        self.manager = ResearchManager(
            caller,
            briefing=BriefingBuilder(
                budget=lambda: {
                    "cycles_used": self.state.cycle,
                    "cycles_remaining": max(
                        0, self.project.config.limits.max_cycles - self.state.cycle
                    ),
                    "experiments_used": self.state.experiments_started,
                    "experiments_remaining": max(
                        0,
                        self.project.config.limits.max_experiments - self.state.experiments_started,
                    ),
                }
            ),
        )
        self.dispatcher = ResearchDispatcher(ResearchAgent(caller))
        self.critic = LLMCritic(caller)
        self.reviews = CriticState(store)
        self.state = RuntimeState.model_validate(store.runtime_get(project_id, "cursor") or {})

    @property
    def project(self):
        return self.store.get(m.Project, self.project_id)

    def _save(self):
        self.store.runtime_put(self.project_id, "cursor", self.state.model_dump(mode="json"))

    def _record_outcome(self, outcome):
        with self.store.transaction():
            self.writer.record_task(outcome.task)
            self.store.runtime_put(
                self.project_id, f"outcome:{outcome.task.id}", outcome.model_dump(mode="json")
            )

    def _decision(self, kind, reason, *, action=None, actor="rm"):
        self.store.create(
            m.Decision(
                id=uuid4(),
                created_at=now(),
                project_id=self.project_id,
                cycle=self.state.cycle,
                decision_type=kind,
                actor=actor,
                summary=kind,
                rationale=reason,
                references=[],
                action=action,
            )
        )

    def _gate(self, question, *, proposed=None, target=None, options=None):
        self.state.gate = HumanGate(
            id=uuid4(),
            question=question,
            proposed_question=proposed,
            protected_target=target,
            options=options or [],
        )
        project = self.project
        project.status = "paused"
        self.store.update(project)

    def pause(self):
        with self.store.transaction():
            project = self.project
            if project.status == "active":
                project.status = "paused"
                self.store.update(project)
                self._decision("human_pause", "Paused at next durable transition", actor="human")

    def resume(self):
        with self.store.transaction():
            # Refresh cursor: pause/resume may be issued by a separate CLI process.
            self.state = RuntimeState.model_validate(
                self.store.runtime_get(self.project_id, "cursor") or {}
            )
            if self.state.gate or self.project.proposed_main_research_question:
                raise StateError("Answer the pending human gate before resuming")
            project = self.project
            if project.status != "paused":
                raise StateError("Only a paused project can resume")
            project.status = "active"
            self.store.update(project)
            self._decision("human_resume", "Human resumed research", actor="human")

    def answer(self, gate_id: UUID, answer: str, *, approve=False, edited_question=None):
        """Trusted human entry point. Never route ManagerAction to this method."""
        if not answer.strip():
            raise ValueError("Human answer must not be empty")
        with self.store.transaction():
            self.state = RuntimeState.model_validate(
                self.store.runtime_get(self.project_id, "cursor") or {}
            )
            gate = self.state.gate
            if gate is None or gate.id != gate_id:
                raise StateError("No matching pending gate (stale answer)")
            if (
                gate.protected_target
                and gate.protected_target != "main_research_question"
                and approve
            ):
                raise StateError(
                    "This protected change needs explicit project reconfiguration; "
                    "baseline changes use baseline refresh"
                )
            if gate.proposed_question is not None:
                question = gate.proposed_question
                if edited_question is not None:
                    if not approve or not edited_question.strip():
                        raise StateError("An edited question requires explicit approval")
                    project = self.project
                    if project.proposed_main_research_question != question:
                        raise StateError("Proposal changed since this gate opened")
                    question = edited_question.strip()
                    project.proposed_main_research_question = question
                    self.store.update(project)
                self.writer.answer_main_question(
                    self.project_id,
                    proposed_question=question,
                    approve=approve,
                    rationale=answer,
                    cycle=self.state.cycle,
                )
            else:
                project = self.project
                project.status = "active"
                self.store.update(project)
            self._decision("human_answer", answer, actor="human")
            # Plans made under the old human context must be reconsidered by RM.
            self.state.actions = []
            self.state.gate = None
            self.state.event = "human_answered"
            self.state.stagnant_cycles = 0
            self._save()

    def _progress(self):
        return [
            len(self.store.list(m.Hypothesis, project_id=self.project_id)),
            len(self.store.list(m.Observation, project_id=self.project_id)),
            len(self.store.list(m.Subproblem, project_id=self.project_id, status="resolved")),
        ]

    def _begin(self, kind, action=None, run_id=None):
        self.state.operation = Operation(
            kind=kind, id=uuid4(), started_at=now(), action=action, run_id=run_id
        )
        self._save()
        return self.state.operation

    def _phase(self, experiment_id, phase):
        target = {
            "implementing": "implementing",
            "implemented": "implemented",
            "building": "testing",
            "testing": "testing",
            "running": "running",
        }.get(phase)
        if target:
            exp = self.store.get(m.Experiment, experiment_id)
            if exp.status != target:
                exp.status = target
                self.store.update(exp)

    def _local(self, cls, ident):
        entity = self.store.get(cls, ident)
        if not any(e.id == ident for e in self.store.list(cls, project_id=self.project_id)):
            raise StateError("Action references another project")
        return entity

    def _execution_result(self, result):
        result = ExperimentResult.model_validate_json(result.model_dump_json())
        op = self.state.operation
        if result.run_id != op.run_id or result.experiment_id != op.action.action.experiment_id:
            raise StateError("Execution result identity mismatch")
        run = self.store.get(m.Run, result.run_id)
        self.store.update(
            m.Run.model_validate({**run.model_dump(), "status": result.status, "result": result})
        )
        exp = self.store.get(m.Experiment, result.experiment_id)
        if result.status == "succeeded":
            # Recovery may see the final manifest before phase updates have committed.
            for phase in ["implemented", "testing", "running"]:
                if (
                    exp.status
                    == {
                        "implemented": "implementing",
                        "testing": "implemented",
                        "running": "testing",
                    }[phase]
                ):
                    exp.status = phase
                    self.store.update(exp)
            self.state.evaluation_run = run.id
        else:
            target = {
                "implementing": "implementation_failed",
                "implemented": "cancelled",
                "testing": "test_failed",
                "running": "run_failed",
            }.get(exp.status, "cancelled")
            if result.failure.kind == "timeout" and exp.status != "implemented":
                target = "timeout"
            elif result.failure.kind == "build_failure":
                target = "implementation_failed"
            exp.status = target
            self.store.update(exp)
            self.state.event = "component_failed"

    def _failed_task(self, ident, kind, reason, *, started=None, resource_class="llm"):
        try:
            task = self.store.get(m.Task, ident)
        except KeyError:
            task = m.Task(
                id=ident,
                project_id=self.project_id,
                created_at=started or now(),
                kind=kind,
                resource_class=resource_class,
                references=[],
            )
        if task.status in ("pending", "running"):
            task = m.Task.model_validate(
                {**task.model_dump(), "status": "failed", "finished_at": now(), "failure": reason}
            )
            self.writer.record_task(task)
        return task

    async def run(self, *, max_transitions: int | None = None):
        """Drain completed events; no polling. max_transitions permits bounded host stepping."""
        if max_transitions is not None and max_transitions < 1:
            raise ValueError("max_transitions must be positive")
        if not self.project.main_question_approved:
            raise StateError("Human must approve the main question")
        if self.project.status != "active":
            if self.state.operation:
                from argos.execution.process import recover_processes

                recover_processes(self.executor.storage.parent)
            return self.state
        if self.state.operation:
            self.recover()
        steps = 0
        while self.project.status == "active":
            if max_transitions is not None and steps >= max_transitions:
                break
            if self.state.evaluation_run:
                await self._evaluate()
            elif self.state.actions:
                await self._action(self.state.actions[0])
            else:
                limits = self.project.config.limits
                if self.state.cycle >= limits.max_cycles:
                    with self.store.transaction():
                        self._gate("Maximum research decision cycles reached")
                        self._save()
                    break
                await self._plan()
            steps += 1
        return self.state

    async def _plan(self):
        with self.store.transaction():
            self.state.cycle += 1
            op = self._begin("manager")
        snapshot = self.store.snapshot(self.project_id, limit=8)
        if self.state.batch:
            outcome = await self.manager.synthesize(snapshot, self.state.batch, task_id=op.id)
        else:
            outcome = await self.manager.plan(snapshot, task_id=op.id, event=self.state.event)
        with self.store.transaction():
            self._accept_plan(outcome)
            self._save()

    def _accept_plan(self, outcome):
        self.state.operation = None
        if outcome.output:
            self.state.batch = None
            self.state.actions = outcome.output.actions
            self.state.event = "actions_recorded"
        else:
            self.state.event = "component_failed"
        progress = self._progress()
        self.state.stagnant_cycles = (
            self.state.stagnant_cycles + 1 if progress == self.state.progress else 0
        )
        self.state.progress = progress
        if self.state.stagnant_cycles >= self.project.config.limits.stagnation_cycles:
            self._gate(
                "No new hypothesis, observation or resolved subproblem; human guidance needed"
            )

    async def _action(self, action):
        kind = action.action.action_type
        if action.project_id != self.project_id:
            raise StateError("Cross-project action")
        try:
            if kind == "dispatch_research_agents":
                with self.store.transaction():
                    self.writer.apply(action, cycle=self.state.cycle)
                    self._begin("research", action)
                batch = await self.dispatcher.dispatch(action)
                with self.store.transaction():
                    self.state.batch = batch
                    self._finish_action()
                return
            if kind in ("implement_experiment", "run_experiment"):
                await self._execute(action)
                return
            if kind == "request_critic_review":
                self._local(m.Claim, action.action.claim_id)
                try:
                    diffs = {
                        row.run.id: Path(row.run.result.diff_path).read_text()
                        for row in self.store.claim_provenance(action.action.claim_id)
                    }
                except OSError as exc:
                    raise ValueError(f"Review evidence unavailable: {exc}") from exc
                context = self.reviews.prepare(action.action.claim_id, code_diffs=diffs)
                with self.store.transaction():
                    op = self._begin("critic", action)
                    self.store.runtime_put(
                        self.project_id, f"critic:{op.id}", context.model_dump(mode="json")
                    )
                outcome = await self.critic.review(context, task_id=op.id)
                with self.store.transaction():
                    self.reviews.record(context, outcome)
                    self._decision(kind, action.rationale, action=action)
                    self.state.event = "review_recorded" if outcome.output else "component_failed"
                    self._finish_action()
                return
            with self.store.transaction():
                if kind == "stop":
                    project = self.project
                    project.status = "completed"
                    self.store.update(project)
                    self._decision(kind, action.rationale, action=action)
                elif kind in ("pause_for_human", "propose_protected_change"):
                    payload = action.action
                    target = getattr(payload, "target", None)
                    proposed = (
                        payload.proposed_change if target == "main_research_question" else None
                    )
                    if proposed:
                        project = self.project
                        project.proposed_main_research_question = proposed
                        self.store.update(project)
                    self._gate(
                        getattr(payload, "question_for_human", None) or payload.proposed_change,
                        proposed=proposed,
                        target=target,
                        options=getattr(payload, "options", []),
                    )
                    self._decision(kind, action.rationale, action=action)
                elif kind == "propose_main_question_revision":
                    self.writer.apply(action, cycle=self.state.cycle)
                    self._gate(
                        "Approve proposed main research question?",
                        proposed=action.action.proposed_question,
                        target="main_research_question",
                    )
                elif kind == "synthesize":
                    outcomes = []
                    for ident in action.action.task_ids:
                        raw = self.store.runtime_get(self.project_id, f"outcome:{ident}")
                        if raw is None:
                            raise StateError("Synthesis requires recorded research outcomes")
                        outcome = AgentOutcome[ResearchAgentResult].model_validate(raw)
                        if (
                            outcome.task.project_id != self.project_id
                            or outcome.task.kind != "research_agent"
                        ):
                            raise StateError("Synthesis requires this project's research tasks")
                        outcomes.append(outcome)
                    if len({o.task.id for o in outcomes}) != len(outcomes):
                        raise StateError("Duplicate synthesis task")
                    self.state.batch = ResearchBatch(outcomes=outcomes)
                    self._decision(kind, action.rationale, action=action)
                elif kind == "continue_research":
                    self._decision(kind, action.rationale, action=action)
                else:
                    self.writer.apply(action, cycle=self.state.cycle)
                self._finish_action()
        except (TaskRecordingError, asyncio.CancelledError):
            raise
        except (ValueError, KeyError) as exc:
            if self.state.operation:
                raise  # Keep uncertain external work recoverable; never orphan its Run.
            # State/semantic errors reject this action, not the rest of the research process.
            # SQLite/persistence errors intentionally propagate with the cursor intact.
            with self.store.transaction():
                self._failed_task(uuid4(), f"action:{kind}", f"{type(exc).__name__}: {exc}")
                self.state.event = "component_failed"
                self._finish_action()

    def _finish_action(self):
        self.state.actions = self.state.actions[1:]
        self.state.operation = None
        self._save()

    async def _execute(self, action):
        exp = self._local(m.Experiment, action.action.experiment_id)
        hypothesis = self.store.get(m.Hypothesis, exp.spec.hypothesis_id)
        if hypothesis.status in ("rejected", "contradicted"):
            raise StateError(
                "Do not repeat a rejected/contradicted direction without human reconsideration"
            )
        if exp.status != "planned":
            raise StateError("Execution requires a planned experiment; retries require a new spec")
        if self.state.experiments_started >= self.project.config.limits.max_experiments:
            with self.store.transaction():
                self._gate("Maximum experiment count reached")
                self._save()
            return
        with self.store.transaction():
            self.state.experiments_started += 1
            ident = uuid4()
            op = self._begin("execute", action, ident)
            self.store.create(
                m.Run(id=ident, created_at=op.started_at, experiment_id=exp.id, status="running")
            )
            self._decision(action.action.action_type, action.rationale, action=action)
            self._phase(exp.id, "implementing")
        result = await self.executor.execute(
            exp.spec, run_id=ident, record_phase=lambda phase: self._phase(exp.id, phase)
        )
        with self.store.transaction():
            self._execution_result(result)
            self._finish_action()

    async def _evaluate(self):
        run = self.store.get(m.Run, self.state.evaluation_run)
        exp = self.store.get(m.Experiment, run.experiment_id)
        with self.store.transaction():
            op = self._begin("evaluate", run_id=run.id)
            exp.status = "evaluating"
            self.store.update(exp)
        try:
            baseline = (
                self.store.get(m.Baseline, exp.spec.baseline_id) if exp.spec.baseline_id else None
            )
            evaluation = await self.evaluator.evaluate(exp.spec, run.result, baseline=baseline)
            evaluation = self._validate_evaluation(evaluation, run, exp)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            evaluation = self._invalid_evaluation(run, exp, str(exc) or repr(exc))
        with self.store.transaction():
            record_evaluation(self.store, evaluation)
            if evaluation.status == "invalid":
                self._failed_task(
                    op.id,
                    "evaluation",
                    str(evaluation.failure),
                    resource_class=exp.spec.resource_class,
                )
            self.state.event = (
                "observations_recorded" if evaluation.status == "ok" else "component_failed"
            )
            self.state.evaluation_run = None
            self.state.operation = None
            self._save()

    def _validate_evaluation(self, evaluation, run, exp):
        evaluation = EvaluatorResult.model_validate_json(evaluation.model_dump_json())
        if (evaluation.run_id, evaluation.experiment_id) != (run.id, exp.id):
            raise ValueError("Evaluation identity mismatch")
        if evaluation.baseline_id != exp.spec.baseline_id:
            raise ValueError("Evaluation must use the experiment's pinned baseline")
        if evaluation.protocol_name != exp.spec.evaluation_protocol.name:
            raise ValueError("Evaluation protocol does not match experiment")
        return evaluation

    def _invalid_evaluation(self, run, exp, reason):
        return EvaluatorResult(
            experiment_id=exp.id,
            run_id=run.id,
            evaluated_at=now(),
            status="invalid",
            baseline_id=exp.spec.baseline_id,
            protocol_name=exp.spec.evaluation_protocol.name,
            measurements=[],
            constraint_checks=[],
            artifacts=[],
            failure=ExecutionFailure(kind="invalid_evaluator_output", message=reason),
        )

    def recover(self):
        """Reconcile a dead runner; caller must hold the exclusive runner lock."""
        op = self.state.operation
        if not op:
            return
        from argos.execution.process import recover_processes

        recover_processes(self.executor.storage.parent)
        reason = "Runner interrupted; no automatic retry. Retained evidence needs inspection."
        with self.store.transaction():
            if op.kind == "manager":
                raw = self.store.runtime_get(self.project_id, f"outcome:{op.id}")
                if raw:
                    self._accept_plan(AgentOutcome[ManagerPlan].model_validate(raw))
                else:
                    self._failed_task(op.id, "manager", reason, started=op.started_at)
            elif op.kind == "research":
                outcomes = []
                for task in op.action.action.tasks:
                    raw = self.store.runtime_get(self.project_id, f"outcome:{task.id}")
                    if raw:
                        outcomes.append(AgentOutcome[ResearchAgentResult].model_validate(raw))
                    else:
                        failed = self._failed_task(
                            task.id, "research_agent", reason, started=op.started_at
                        )
                        outcomes.append(AgentOutcome(task=failed))
                self.state.batch = ResearchBatch(outcomes=outcomes)
                self.state.actions = self.state.actions[1:]
            elif op.kind == "execute":
                exp = self.store.get(m.Experiment, op.action.action.experiment_id)
                evidence = self.executor.storage / str(exp.id) / str(op.run_id) / "evidence"
                manifest = evidence / "result.json"
                if manifest.exists():
                    result = ExperimentResult.model_validate_json(manifest.read_text())
                else:
                    result = interrupted_result(
                        exp.spec,
                        op.run_id,
                        op.started_at,
                        self.project.config,
                        self.executor.worktrees,
                        evidence,
                        reason,
                    )
                self._execution_result(result)
                self.state.actions = self.state.actions[1:]
            elif op.kind == "evaluate":
                run = self.store.get(m.Run, op.run_id)
                exp = self.store.get(m.Experiment, run.experiment_id)
                manifests = list((self.evaluator.storage / str(run.id)).glob("*/evaluation.json"))
                evaluation = self._invalid_evaluation(run, exp, reason)
                if len(manifests) == 1:
                    try:
                        saved = EvaluatorResult.model_validate_json(manifests[0].read_text())
                        evaluation = self._validate_evaluation(saved, run, exp)
                    except ValueError as exc:
                        evaluation = self._invalid_evaluation(run, exp, str(exc))
                record_evaluation(self.store, evaluation)
                if evaluation.status == "invalid":
                    self._failed_task(
                        op.id,
                        "evaluation",
                        str(evaluation.failure),
                        resource_class=exp.spec.resource_class,
                    )
                self.state.evaluation_run = None
            elif op.kind == "critic":
                from argos.protocols import CriticReview
                from argos.research import CriticInput

                raw = self.store.runtime_get(self.project_id, f"outcome:{op.id}")
                if raw:
                    context = CriticInput.model_validate(
                        self.store.runtime_get(self.project_id, f"critic:{op.id}")
                    )
                    self.reviews.record(context, AgentOutcome[CriticReview].model_validate(raw))
                else:
                    self._failed_task(op.id, "critic", reason, started=op.started_at)
                self.state.actions = self.state.actions[1:]
            self._decision("restart_recovery", reason)
            self.state.operation = None
            self.state.event = "recovered"
            self._save()
