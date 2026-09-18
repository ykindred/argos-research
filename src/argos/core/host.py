"""Trusted local host setup and baseline initialization, never agent capabilities."""

import fcntl
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from argos import models as m
from argos.common import ExecutionFailure
from argos.evaluation import BaselineRunner, CommandEvaluator, approve_baseline, record_evaluation
from argos.execution import ExperimentAgent
from argos.execution.process import recover_processes
from argos.protocols import EvaluatorResult, ExperimentResult, ExperimentSpec
from argos.state import StateError

from .orchestrator import Orchestrator, now
from .recovery import interrupted_result


@contextmanager
def runner_lock(directory: Path):
    """OS releases the advisory lock when the runner dies; status/pause stay available."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "runner.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("Another local runner owns this project") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def build_runtime(store, project, directory, llm, coding):
    executor = ExperimentAgent(project.config, coding, directory / "execution")
    evaluator = CommandEvaluator(
        project.config, directory / "evaluation", cpu_sem=executor.cpu_sem, gpu_sem=executor.gpu_sem
    )
    return Orchestrator(store, project.id, llm, executor, evaluator)


async def refresh_baseline(store, project, directory, *, rationale):
    """Explicit human operation, including reconciliation of an interrupted baseline.

    Re-entering after a crash imports a completed manifest or records failure; it
    never silently repeats an interrupted command. A later refresh starts a new run.
    """
    config = project.config
    pending = store.runtime_get(project.id, "baseline_cursor")
    runner = BaselineRunner(config, directory / "baselines")
    if pending:
        recover_processes(directory)
        exp = store.get(m.Experiment, pending["experiment_id"])
        spec = exp.spec
        run = store.get(m.Run, pending["run_id"])
    else:
        timestamp = now()
        subproblem = m.Subproblem(
            id=uuid4(),
            created_at=timestamp,
            updated_at=timestamp,
            project_id=project.id,
            question="Human baseline measurement",
        )
        hypothesis = m.Hypothesis(
            id=uuid4(),
            created_at=timestamp,
            updated_at=timestamp,
            subproblem_id=subproblem.id,
            statement="Measure the unchanged source",
            rationale="Human-controlled baseline, not a scientific conclusion",
        )
        spec = ExperimentSpec(
            experiment_id=uuid4(),
            hypothesis_id=hypothesis.id,
            hypothesis_statement=hypothesis.statement,
            goal="Measure clean baseline",
            prediction="The unchanged project passes its protected protocol",
            success_criteria=["Valid measurement with passing constraints"],
            failure_criteria=["Invalid measurement or failed constraint"],
            requested_change="No source changes",
            build_steps=[config.build_command],
            test_steps=[config.test_command],
            run_steps=[config.test_command],
            evaluation_protocol=config.evaluation_protocol,
            resource_limits=config.resource_limits,
            scope=config.scope,
        )
        run = m.Run(
            id=uuid4(), created_at=timestamp, experiment_id=spec.experiment_id, status="running"
        )
        with store.transaction():
            store.create(subproblem)
            store.create(hypothesis)
            store.create(
                m.Experiment(
                    id=spec.experiment_id, created_at=timestamp, spec=spec, status="implementing"
                )
            )
            store.create(run)
            store.runtime_put(
                project.id,
                "baseline_cursor",
                {
                    "experiment_id": str(spec.experiment_id),
                    "run_id": str(run.id),
                    "phase": "execute",
                },
            )
    if run.result is None:
        if pending:
            evidence = runner.storage / str(run.id) / "evidence"
            manifest = evidence / "result.json"
            result = (
                ExperimentResult.model_validate_json(manifest.read_text())
                if manifest.exists()
                else interrupted_result(
                    spec,
                    run.id,
                    run.created_at,
                    config,
                    runner.worktrees,
                    evidence,
                    "Baseline interrupted",
                )
            )
        else:
            result = await runner.execute(spec, run_id=run.id)
        if (result.run_id, result.experiment_id) != (run.id, spec.experiment_id):
            raise StateError("Baseline manifest identity mismatch")
        with store.transaction():
            run = store.update(
                m.Run.model_validate(
                    {**run.model_dump(), "status": result.status, "result": result}
                )
            )
            exp = store.get(m.Experiment, spec.experiment_id)
            if result.status == "succeeded":
                for phase in ["implemented", "running"]:
                    exp.status = phase
                    store.update(exp)
            else:
                exp.status = (
                    "timeout" if result.failure.kind == "timeout" else "implementation_failed"
                )
                store.update(exp)
                store.runtime_put(project.id, "baseline_cursor", {})
    if run.status != "succeeded":
        raise StateError(
            "Baseline failed; inspect retained baseline evidence, then refresh explicitly"
        )
    if run.evaluation is None:
        evaluator = CommandEvaluator(
            config,
            directory / "baseline-evaluation",
            cpu_sem=runner.cpu_sem,
            gpu_sem=runner.gpu_sem,
        )
        if pending and pending["phase"] == "evaluate":
            manifests = list((evaluator.storage / str(run.id)).glob("*/evaluation.json"))
            if len(manifests) == 1:
                evaluation = EvaluatorResult.model_validate_json(manifests[0].read_text())
            else:
                evaluation = EvaluatorResult(
                    experiment_id=spec.experiment_id,
                    run_id=run.id,
                    evaluated_at=now(),
                    status="invalid",
                    protocol_name=config.evaluation_protocol.name,
                    measurements=[],
                    constraint_checks=[],
                    artifacts=[],
                    failure=ExecutionFailure(
                        kind="invalid_evaluator_output", message="Baseline evaluation interrupted"
                    ),
                )
        else:
            store.runtime_put(
                project.id,
                "baseline_cursor",
                {
                    "experiment_id": str(spec.experiment_id),
                    "run_id": str(run.id),
                    "phase": "evaluate",
                },
            )
            evaluation = await evaluator.evaluate(spec, run.result)
        if (evaluation.run_id, evaluation.experiment_id) != (run.id, spec.experiment_id):
            raise StateError("Baseline evaluation identity mismatch")
        record_evaluation(store, evaluation)
        run = store.get(m.Run, run.id)
    if run.evaluation.status != "ok" or any(not c.passed for c in run.evaluation.constraint_checks):
        store.runtime_put(project.id, "baseline_cursor", {})
        raise StateError(
            "Baseline evaluation failed; evidence retained, current baseline unchanged"
        )
    with store.transaction():
        baseline = approve_baseline(store, project.id, run.id, rationale=rationale)
        store.runtime_put(project.id, "baseline_cursor", {})
        # Initial failed setup stays paused until a valid human baseline is established.
        current = store.get(m.Project, project.id)
        if project.baseline_id is None and current.status == "paused":
            current.status = "active"
            store.update(current)
        return baseline
