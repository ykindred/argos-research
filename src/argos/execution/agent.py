"""EA implementation boundary followed by deterministic local execution."""

import asyncio
import fnmatch
import json
import math
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from argos.backends.coding import CodingBackend
from argos.common import ExecutionFailure, PathScope
from argos.protocols import (
    CodingResult,
    CodingTask,
    ExperimentResult,
    ExperimentSpec,
    ProjectConfig,
)

from .process import ProcessCancelled, ProcessRunner
from .worktree import WorktreeManager


def relative_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in name:
        raise ValueError(f"Unsafe repository-relative path: {name}")
    if not path.parts:
        raise ValueError("Empty path is not allowed")
    return path


def matches(name: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(name, p) or name.startswith(p.rstrip("/") + "/") for p in patterns
    )


def safe_file(root: Path, name: str) -> Path:
    relative_path(name)
    path = root / name
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Symlink not allowed in evidence path: {name}")
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"Missing regular file: {name}")
    return path


class ExecutionError(Exception):
    def __init__(self, kind: str, message: str):
        self.failure = ExecutionFailure(kind=kind, message=message)
        super().__init__(message)


class ExperimentAgent:
    """One execute call is one attempt; no automatic coding retries or evaluation.

    Share an instance to share finite coding/CPU/GPU queues. The host owns SQLite
    transitions; result.json and events.jsonl make this execution independently
    inspectable. storage must be outside the canonical repository.
    """

    def __init__(
        self,
        project: ProjectConfig,
        backend: CodingBackend,
        storage: Path,
        *,
        command_timeout_seconds: float | None = None,
    ):
        self.project = ProjectConfig.model_validate_json(project.model_dump_json())
        self.backend = backend
        self.worktrees = WorktreeManager(Path(self.project.source_repository))
        self.storage = storage.resolve()
        if self.storage.is_relative_to(self.worktrees.repository):
            raise ValueError("Execution storage must be outside the source checkout")
        if command_timeout_seconds is not None and (
            not math.isfinite(command_timeout_seconds) or command_timeout_seconds <= 0
        ):
            raise ValueError("Command timeout must be finite and positive")
        self.command_timeout = command_timeout_seconds
        self.process = ProcessRunner()
        self.coding_sem = asyncio.Semaphore(self.project.resources.coding_slots)
        self.cpu_sem = asyncio.Semaphore(self.project.resources.cpu_jobs)
        self.gpu_sem = asyncio.Semaphore(self.project.resources.gpu_jobs)

    async def execute(
        self,
        spec: ExperimentSpec,
        *,
        run_id: UUID | None = None,
        record_phase: Callable[[str], None] | None = None,
    ) -> ExperimentResult:
        # Revalidate even instances: callers can mutate nested lists or bypass constructors.
        spec = ExperimentSpec.model_validate_json(spec.model_dump_json())
        started = datetime.now(UTC)
        run_id = run_id or uuid4()
        attempt = self.storage / str(spec.experiment_id) / str(run_id)
        evidence = attempt / "evidence"
        evidence.mkdir(parents=True, exist_ok=False)
        workspace = attempt / "worktree"
        for name in ["code.diff", "stdout.log", "stderr.log"]:
            (evidence / name).touch()
        source = "unavailable"
        revision = None
        commands = []
        artifacts = []
        failure = None
        phase = "planned"
        created = False
        config = {
            "project": self.project.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"),
            "command_timeout_seconds": self.command_timeout,
        }
        (evidence / "configuration.json").write_text(json.dumps(config, indent=2))

        def event(status: str):
            nonlocal phase
            phase = status
            if record_phase:
                record_phase(status)
            with (evidence / "events.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"status": status, "at": datetime.now(UTC).isoformat()}) + "\n"
                )

        def check_changes(names: list[str], *, implementation: bool):
            scopes = [self.project.scope] + ([spec.scope] if spec.scope else [])
            for name in names:
                relative_path(name)
                if any(matches(name, s.protected_paths) for s in scopes):
                    raise ExecutionError("invalid_modification", f"Protected path changed: {name}")
                if implementation and not all(matches(name, s.editable_paths) for s in scopes):
                    raise ExecutionError(
                        "invalid_modification", f"Path outside editable scope: {name}"
                    )
                path = workspace / name
                if path.exists() or path.is_symlink():
                    try:
                        safe_file(workspace, name)
                    except ValueError as exc:
                        raise ExecutionError("invalid_modification", str(exc)) from exc

        event("planned")
        try:
            source = self.worktrees.source_commit()
            config["source_commit"] = source
            (evidence / "configuration.json").write_text(json.dumps(config, indent=2))
            if spec.evaluation_protocol != self.project.evaluation_protocol:
                raise ExecutionError("invalid_modification", "Experiment cannot replace evaluator")
            for scope in [self.project.scope, spec.scope]:
                if scope:
                    for pattern in scope.editable_paths + scope.protected_paths:
                        relative_path(pattern)
            for name in spec.required_artifacts:
                relative_path(name)
            timeout = min(
                spec.resource_limits.timeout_seconds, self.project.resource_limits.timeout_seconds
            )
            # Includes queue waits, implementation and all commands in one wall-clock budget.
            async with asyncio.timeout(timeout):
                self.worktrees.create(workspace, source)
                created = True
                if (workspace / ".argos-coding").exists():
                    raise ExecutionError(
                        "invalid_modification", "Reserved diagnostics path exists in source"
                    )
                event("implementing")
                scope = spec.scope or self.project.scope
                task = CodingTask(
                    task_id=uuid4(),
                    experiment_id=spec.experiment_id,
                    requested_change=spec.requested_change,
                    project_scope=self.project.scope.model_copy(deep=True),
                    scope=PathScope(
                        editable_paths=scope.editable_paths,
                        protected_paths=list(
                            dict.fromkeys(
                                self.project.scope.protected_paths + scope.protected_paths
                            )
                        ),
                    ),
                )
                async with self.coding_sem:
                    raw = await self.backend.run(workspace, task, timeout)
                coding = CodingResult.model_validate_json(raw.model_dump_json())
                if coding.task_id != task.task_id or coding.experiment_id != spec.experiment_id:
                    raise ExecutionError(
                        "implementation_failure", "Coding result identity mismatch"
                    )
                # Retain diagnostics only from the assigned attempt, never arbitrary paths.
                for i, name in enumerate(coding.log_paths):
                    path = Path(name)
                    if not path.is_absolute():
                        path = workspace / path
                    try:
                        local = str(path.relative_to(attempt))
                        log = safe_file(attempt, local)
                    except ValueError as exc:
                        raise ExecutionError("implementation_failure", str(exc)) from exc
                    target = evidence / f"coding-{i}.log"
                    shutil.copyfile(log, target)
                    artifacts.append(str(target))
                if coding.status != "implemented":
                    raise ExecutionError(coding.failure.kind, coding.failure.message)
                if self.worktrees.git(workspace, "rev-parse", "HEAD").decode().strip() != source:
                    raise ExecutionError("invalid_modification", "Coding backend changed Git HEAD")
                tree, changed = self.worktrees.snapshot(workspace, source, evidence)
                check_changes(changed, implementation=True)
                revision = self.worktrees.record_revision(workspace, source, tree)
                event("implemented")
                sem = self.gpu_sem if spec.resource_class == "gpu" else self.cpu_sem
                async with sem:
                    for label, steps, kind in [
                        ("building", spec.build_steps, "build_failure"),
                        ("testing", spec.test_steps, "test_failure"),
                        ("running", spec.run_steps, "runtime_crash"),
                    ]:
                        if steps:
                            event(label)
                        for argv in steps:
                            index = len(commands)
                            out = evidence / f"command-{index}.stdout"
                            err = evidence / f"command-{index}.stderr"
                            # Persist attempted argv before launching, including cancellation cases.
                            (evidence / f"command-{index}.json").write_text(json.dumps(argv))
                            try:
                                outcome = await self.process.run(
                                    argv,
                                    workspace,
                                    min(self.command_timeout or timeout, timeout),
                                    out,
                                    err,
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
                            if outcome.timed_out:
                                raise ExecutionError("timeout", f"{label} command timed out")
                            if outcome.invalid_command:
                                raise ExecutionError("invalid_command", f"Cannot execute {argv[0]}")
                            if outcome.record.exit_code != 0:
                                raise ExecutionError(
                                    kind, f"{label} command exited {outcome.record.exit_code}"
                                )
                # Commands may create outputs, but may not alter the implementation or protocol.
                _, changed = self.worktrees.snapshot(workspace, source, evidence)
                check_changes(changed, implementation=False)
                if self.worktrees.git(workspace, "diff", "--name-only", revision, "--"):
                    raise ExecutionError(
                        "invalid_modification", "Commands modified recorded source files"
                    )
                for name in spec.required_artifacts:
                    try:
                        path = safe_file(workspace, name)
                    except ValueError as exc:
                        raise ExecutionError("missing_artifact", str(exc)) from exc

        except ExecutionError as exc:
            failure = exc.failure
        except TimeoutError:
            failure = ExecutionFailure(
                kind="timeout", message=f"Experiment timed out during {phase}"
            )
        except asyncio.CancelledError:
            failure = ExecutionFailure(
                kind="implementation_failure", message=f"Cancelled during {phase}"
            )
            raise
        except Exception as exc:
            failure = ExecutionFailure(
                kind="implementation_failure", message=f"{phase}: {type(exc).__name__}: {exc}"
            )
        finally:
            if created:
                try:
                    self.worktrees.snapshot(workspace, source, evidence)
                except Exception as exc:
                    (evidence / "preservation-error.log").write_text(str(exc))
                    failure = failure or ExecutionFailure(
                        kind="implementation_failure", message=f"Cannot preserve final diff: {exc}"
                    )
            if created:
                # Retain backend diagnostics even when it raised or was cancelled
                # before returning a CodingResult. Never follow diagnostic symlinks.
                diagnostics = workspace / ".argos-coding"
                if diagnostics.is_dir() and not diagnostics.is_symlink():
                    for candidate in diagnostics.rglob("*"):
                        if not candidate.is_file():
                            continue
                        try:
                            name = str(candidate.relative_to(workspace))
                            path = safe_file(workspace, name)
                        except ValueError:
                            continue
                        target = (
                            evidence / "backend-diagnostics" / candidate.relative_to(diagnostics)
                        )
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                        artifacts.append(str(target))
                for name in spec.required_artifacts:
                    try:
                        path = safe_file(workspace, name)
                        target = evidence / "artifacts" / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(path, target)
                        if str(target) not in artifacts:
                            artifacts.append(str(target))
                    except ValueError:
                        pass  # Missing/unsafe outputs do not erase the original failure.
            # Keep both raw per-command files and aggregate streams even on failure.
            (evidence / "stdout.log").write_text("".join(c.stdout for c in commands))
            (evidence / "stderr.log").write_text("".join(c.stderr for c in commands))
            event("failed" if failure else "executed")
            result = ExperimentResult(
                experiment_id=spec.experiment_id,
                run_id=run_id,
                status="failed" if failure else "succeeded",
                started_at=started,
                finished_at=datetime.now(UTC),
                worktree=str(workspace),
                diff_path=str(evidence / "code.diff"),
                stdout_path=str(evidence / "stdout.log"),
                stderr_path=str(evidence / "stderr.log"),
                source_commit=source,
                resulting_commit=revision,
                configuration=config,
                commands=commands,
                artifacts=artifacts,
                failure=failure,
            )
            temporary = evidence / "result.json.tmp"
            temporary.write_text(result.model_dump_json(indent=2))
            temporary.replace(evidence / "result.json")
        return result
