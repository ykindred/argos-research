"""Clean baseline execution and explicit human initialization/refresh."""

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from argos.common import ExecutionFailure
from argos.execution.agent import safe_file
from argos.execution.process import ProcessCancelled, ProcessRunner
from argos.execution.worktree import WorktreeManager
from argos.models import Baseline, Project, Run
from argos.protocols import ExperimentResult, ExperimentSpec, ProjectConfig
from argos.state import StateError, StateStore


class BaselineRunner:
    """Build/test/run unchanged HEAD in a new worktree, with no CodingBackend.

    The host evaluates the returned successful result using the same Evaluator
    as experiments. Worktrees and failure evidence are retained for inspection.
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
        self.worktrees = WorktreeManager(Path(self.project.source_repository))
        if self.storage.is_relative_to(self.worktrees.repository):
            raise ValueError("Baseline storage must be outside the source checkout")
        self.process = ProcessRunner()
        self.cpu_sem = cpu_sem or asyncio.Semaphore(project.resources.cpu_jobs)
        self.gpu_sem = gpu_sem or asyncio.Semaphore(project.resources.gpu_jobs)

    async def execute(
        self, spec: ExperimentSpec, *, run_id: UUID | None = None
    ) -> ExperimentResult:
        spec = ExperimentSpec.model_validate_json(spec.model_dump_json())
        if spec.evaluation_protocol != self.project.evaluation_protocol:
            raise ValueError("Baseline cannot replace the protected evaluator")
        started = datetime.now(UTC)
        ident = run_id or uuid4()
        attempt = self.storage / str(ident)
        evidence = attempt / "evidence"
        evidence.mkdir(parents=True, exist_ok=False)
        workspace = attempt / "worktree"
        commands = []
        artifacts = []
        source = "unavailable"
        created = False
        failure = None
        cancelled = None
        configuration = {
            "project": self.project.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"),
            "baseline_clean_checkout": True,
        }
        (evidence / "configuration.json").write_text(json.dumps(configuration, indent=2))
        (evidence / "code.diff").touch()
        timeout = min(
            spec.resource_limits.timeout_seconds, self.project.resource_limits.timeout_seconds
        )
        try:
            source = self.worktrees.source_commit()
            configuration["source_commit"] = source
            (evidence / "configuration.json").write_text(json.dumps(configuration, indent=2))
            self.worktrees.create(workspace, source)
            created = True
            # Includes ignored/untracked files; a baseline begins with only committed inputs.
            if self.worktrees.git(workspace, "status", "--porcelain", "--ignored"):
                raise ValueError("Baseline checkout is not clean")
            sem = self.gpu_sem if spec.resource_class == "gpu" else self.cpu_sem
            async with asyncio.timeout(timeout):
                async with sem:
                    for steps, kind in (
                        (spec.build_steps, "build_failure"),
                        (spec.test_steps, "test_failure"),
                        (spec.run_steps, "runtime_crash"),
                    ):
                        for argv in steps:
                            index = len(commands)
                            (evidence / f"command-{index}.json").write_text(json.dumps(argv))
                            try:
                                outcome = await self.process.run(
                                    argv,
                                    workspace,
                                    timeout,
                                    evidence / f"command-{index}.stdout",
                                    evidence / f"command-{index}.stderr",
                                )
                            except ProcessCancelled as exc:
                                commands.append(exc.record)
                                (evidence / f"command-{index}.result.json").write_text(
                                    exc.record.model_dump_json(indent=2)
                                )
                                raise asyncio.CancelledError() from exc
                            commands.append(outcome.record)
                            (evidence / f"command-{index}.result.json").write_text(
                                outcome.record.model_dump_json(indent=2)
                            )
                            if (
                                outcome.timed_out
                                or outcome.invalid_command
                                or outcome.record.exit_code
                            ):
                                failure = ExecutionFailure(
                                    kind=(
                                        "timeout"
                                        if outcome.timed_out
                                        else "invalid_command"
                                        if outcome.invalid_command
                                        else kind
                                    ),
                                    message=f"Baseline command failed: {argv[0]}",
                                )
                                break
                        if failure:
                            break
            if self.worktrees.git(workspace, "diff", "HEAD", "--") or (
                self.worktrees.git(workspace, "rev-parse", "HEAD").decode().strip() != source
            ):
                raise ValueError("Baseline commands changed committed source")
            if not failure:
                for name in spec.required_artifacts:
                    try:
                        safe_file(workspace, name)
                    except ValueError as exc:
                        failure = ExecutionFailure(kind="missing_artifact", message=str(exc))
                        break
        except TimeoutError:
            failure = ExecutionFailure(kind="timeout", message="Baseline execution timed out")
        except asyncio.CancelledError as exc:
            cancelled = exc
            failure = ExecutionFailure(kind="runtime_crash", message="Baseline execution cancelled")
        except Exception as exc:
            failure = ExecutionFailure(kind="runtime_crash", message=str(exc) or repr(exc))
        finally:
            if created:
                try:
                    # Preserve generated/untracked files in the patch as well as tracked changes.
                    self.worktrees.snapshot(workspace, source, evidence)
                except Exception as exc:
                    (evidence / "preservation-error.log").write_text(str(exc))
                    failure = failure or ExecutionFailure(
                        kind="runtime_crash", message=f"Cannot preserve baseline diff: {exc}"
                    )
                for name in spec.required_artifacts:
                    try:
                        source_file = safe_file(workspace, name)
                        target = evidence / "artifacts" / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source_file, target)
                        artifacts.append(str(target))
                    except ValueError:
                        pass
            (evidence / "stdout.log").write_text("".join(c.stdout for c in commands))
            (evidence / "stderr.log").write_text("".join(c.stderr for c in commands))
        result = ExperimentResult(
            experiment_id=spec.experiment_id,
            run_id=ident,
            status="failed" if failure else "succeeded",
            started_at=started,
            finished_at=datetime.now(UTC),
            worktree=str(workspace),
            diff_path=str(evidence / "code.diff"),
            stdout_path=str(evidence / "stdout.log"),
            stderr_path=str(evidence / "stderr.log"),
            source_commit=source,
            resulting_commit=source if created else None,
            configuration=configuration,
            commands=commands,
            artifacts=artifacts,
            failure=failure,
        )
        temporary = evidence / "result.json.tmp"
        temporary.write_text(result.model_dump_json(indent=2))
        temporary.replace(evidence / "result.json")
        if cancelled:
            raise cancelled
        return result


def approve_baseline(
    store: StateStore, project_id: UUID, run_id: UUID, *, rationale: str
) -> Baseline:
    """Trusted human action only; no automatic refresh or agent-facing approval flag."""
    with store.transaction():
        project = store.get(Project, project_id)
        run = store.get(Run, run_id)
        if (
            run.status != "succeeded"
            or run.evaluation is None
            or run.evaluation.status != "ok"
            or run.result.configuration.get("baseline_clean_checkout") is not True
            or run.result.source_commit != run.result.resulting_commit
            or any(not c.passed for c in run.evaluation.constraint_checks)
        ):
            raise StateError("Baseline requires valid, constraint-passing clean baseline execution")
        return store.approve_baseline(
            Baseline(
                id=uuid4(),
                created_at=datetime.now(UTC),
                project_id=project.id,
                run_id=run.id,
                source_commit=run.result.source_commit,
                evaluation=run.evaluation,
                previous_baseline_id=project.baseline_id,
                approval_decision_id=uuid4(),
            ),
            rationale=rationale,
        )
