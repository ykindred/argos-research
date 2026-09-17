"""Deterministic command evaluation after implementation and execution have exited."""

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from argos.common import ExecutionFailure
from argos.execution.agent import safe_file
from argos.execution.process import ProcessCancelled, ProcessRunner
from argos.execution.worktree import WorktreeManager
from argos.models import Baseline
from argos.protocols import (
    EvaluationProvenance,
    EvaluatorResult,
    ExperimentResult,
    ExperimentSpec,
    ProjectConfig,
)

from .result import compare, constraints, measurements, parse_output


class Evaluator(Protocol):
    async def evaluate(
        self, spec: ExperimentSpec, result: ExperimentResult, *, baseline: Baseline | None = None
    ) -> EvaluatorResult: ...


class CommandEvaluator:
    """Run the protected project command with argv, in the experiment worktree.

    Storage is host-owned and outside the worktree. Every invocation gets a new
    evidence directory, including invalid output. Share CPU/GPU semaphores with
    the execution layer when both components can run concurrently.
    """

    def __init__(
        self,
        project: ProjectConfig,
        storage: Path,
        *,
        cpu_sem: asyncio.Semaphore | None = None,
        gpu_sem: asyncio.Semaphore | None = None,
    ):
        self.project = ProjectConfig.model_validate_json(project.model_dump_json())
        self.storage = storage.resolve()
        if self.storage.is_relative_to(Path(self.project.source_repository).resolve()):
            raise ValueError("Evaluation storage must be outside the source checkout")
        self.cpu_sem = cpu_sem or asyncio.Semaphore(self.project.resources.cpu_jobs)
        self.gpu_sem = gpu_sem or asyncio.Semaphore(self.project.resources.gpu_jobs)
        self.process = ProcessRunner()

    async def _output(self, spec, result, evidence, timeout):
        outcome = await self.process.run(
            spec.evaluation_protocol.command,
            Path(result.worktree),
            timeout,
            evidence / "stdout.log",
            evidence / "stderr.log",
        )
        failure = None
        if outcome.timed_out:
            failure = ExecutionFailure(kind="timeout", message="Evaluator command timed out")
        elif outcome.invalid_command or outcome.record.exit_code != 0:
            failure = ExecutionFailure(
                kind="invalid_evaluator_output",
                message=f"Evaluator command failed (exit {outcome.record.exit_code})",
            )
        return outcome.record, failure

    async def evaluate(self, spec, result, *, baseline=None):
        spec = ExperimentSpec.model_validate_json(spec.model_dump_json())
        result = ExperimentResult.model_validate_json(result.model_dump_json())
        if result.status != "succeeded" or result.experiment_id != spec.experiment_id:
            raise ValueError("Evaluation requires matching successful execution")
        if spec.evaluation_protocol != self.project.evaluation_protocol:
            raise ValueError("Experiment cannot replace the protected evaluator")
        recorded_spec = result.configuration.get("spec")
        if recorded_spec is not None and ExperimentSpec.model_validate(recorded_spec) != spec:
            raise ValueError("Evaluation spec differs from recorded execution inputs")
        if (baseline.id if baseline else None) != spec.baseline_id:
            raise ValueError("Evaluation requires the experiment's pinned baseline")
        if baseline:
            baseline = Baseline.model_validate_json(baseline.model_dump_json())
            if baseline.evaluation.protocol_name != spec.evaluation_protocol.name:
                raise ValueError("Baseline protocol does not match experiment")
        workspace = Path(result.worktree).resolve()
        if self.storage.is_relative_to(workspace):
            raise ValueError("Evaluation evidence must be outside the worktree")
        if workspace == Path(self.project.source_repository).resolve():
            raise ValueError("Evaluation requires an isolated worktree")
        evidence = self.storage / str(result.run_id) / str(uuid4())
        evidence.mkdir(parents=True, exist_ok=False)
        (evidence / "input.json").write_text(
            json.dumps(
                {
                    "spec": spec.model_dump(mode="json"),
                    "execution": result.model_dump(mode="json"),
                    "project": self.project.model_dump(mode="json"),
                    "baseline": baseline.model_dump(mode="json") if baseline else None,
                },
                indent=2,
            )
        )
        for name in ("stdout.log", "stderr.log"):
            (evidence / name).touch()
        provenance = EvaluationProvenance(
            protocol=spec.evaluation_protocol,
            parser="argos-json-v1",
            input_path=str(evidence / "input.json"),
            stdout_path=str(evidence / "stdout.log"),
            stderr_path=str(evidence / "stderr.log"),
            worktree=str(workspace),
            source_commit=result.source_commit,
            resulting_commit=result.resulting_commit,
        )
        failure = None
        metrics, checks, comparisons, artifacts = [], [], [], []
        cancelled = None
        timeout = min(
            spec.resource_limits.timeout_seconds, self.project.resource_limits.timeout_seconds
        )
        sem = self.gpu_sem if spec.resource_class == "gpu" else self.cpu_sem

        def check_source():
            git = WorktreeManager(workspace).git
            diff = git(
                workspace, "diff", "--binary", "--no-ext-diff", result.resulting_commit, "--"
            )
            (evidence / "source.diff").write_bytes(diff)
            if git(workspace, "rev-parse", "HEAD").decode().strip() != result.resulting_commit:
                raise ValueError("Evaluation worktree revision does not match execution")
            if diff:
                raise ValueError("Evaluation worktree has modified source")

        try:
            check_source()
            async with asyncio.timeout(timeout):
                async with sem:
                    try:
                        provenance.command, failure = await self._output(
                            spec, result, evidence, timeout
                        )
                    except ProcessCancelled as exc:
                        provenance.command = exc.record
                        raise asyncio.CancelledError() from exc
            check_source()
            if failure is None:
                output = parse_output(provenance.command.stdout, spec.evaluation_protocol)
                metrics, checks = measurements(output), constraints(output)
                if baseline:
                    comparisons = compare(metrics, baseline.evaluation.measurements)
                    required = set(spec.evaluation_protocol.metric_names)
                    if not required <= {c.name for c in comparisons}:
                        raise ValueError("Baseline is missing required metrics")
                for name in output.artifacts:
                    source = safe_file(workspace, name)
                    target = evidence / "artifacts" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    artifacts.append(str(target))
        except asyncio.CancelledError as exc:
            cancelled = exc
            failure = ExecutionFailure(
                kind="invalid_evaluator_output", message="Evaluation cancelled"
            )
        except TimeoutError:
            failure = ExecutionFailure(kind="timeout", message="Evaluation budget exhausted")
        except Exception as exc:
            failure = ExecutionFailure(
                kind="invalid_evaluator_output", message=str(exc) or repr(exc)
            )
        # Retain changes even when a command timed out/cancelled before the post-check.
        try:
            check_source()
        except Exception as exc:
            failure = failure or ExecutionFailure(
                kind="invalid_evaluator_output", message=str(exc) or repr(exc)
            )
        if (evidence / "source.diff").exists():
            artifacts.append(str(evidence / "source.diff"))
        evaluation = EvaluatorResult(
            experiment_id=result.experiment_id,
            run_id=result.run_id,
            evaluated_at=datetime.now(UTC),
            status="invalid" if failure else "ok",
            baseline_id=spec.baseline_id,
            protocol_name=spec.evaluation_protocol.name,
            measurements=[] if failure else metrics,
            constraint_checks=[] if failure else checks,
            comparisons=[] if failure else comparisons,
            artifacts=artifacts,
            provenance=provenance,
            failure=failure,
        )
        (evidence / "evaluation.json").write_text(evaluation.model_dump_json(indent=2))
        if cancelled:
            raise cancelled
        return evaluation


class FakeEvaluator(CommandEvaluator):
    """Fixed project JSON passed through the real parser and evidence pipeline."""

    def __init__(self, project, storage, output: str):
        super().__init__(project, storage)
        self.output = output

    async def _output(self, spec, result, evidence, timeout):
        from argos.common import CommandRecord

        (evidence / "stdout.log").write_text(self.output)
        return CommandRecord(
            argv=["FakeEvaluator"], exit_code=0, stdout=self.output, stderr=""
        ), None
