"""Preserve incomplete attempt evidence before marking interruption as failure."""

import json
import shutil
from datetime import UTC, datetime
from uuid import uuid4

from argos.common import CommandRecord, ExecutionFailure
from argos.execution.agent import safe_file
from argos.protocols import ExperimentResult


def recover_execution_result(spec, run_id, started_at, project_config, worktrees, evidence, reason):
    """Import a valid result, or retain corruption and fail without replaying work."""
    manifest = evidence / "result.json"
    if manifest.exists():
        try:
            return ExperimentResult.model_validate_json(manifest.read_text())
        except ValueError:
            manifest.replace(evidence / f"result.invalid-{uuid4()}.json")
            reason = f"{reason}; invalid execution result manifest retained"
    return interrupted_result(spec, run_id, started_at, project_config, worktrees, evidence, reason)


def interrupted_result(spec, run_id, started_at, project_config, worktrees, evidence, reason):
    evidence.mkdir(parents=True, exist_ok=True)
    for name in ["code.diff", "stdout.log", "stderr.log"]:
        (evidence / name).touch(exist_ok=True)
    workspace = evidence.parent / "worktree"
    config_path = evidence / "configuration.json"
    config = {
        "spec": spec.model_dump(mode="json"),
        "project": project_config.model_dump(mode="json"),
    }
    if config_path.exists():
        config = json.loads(config_path.read_text())
    source = config.get("source_commit", "unavailable")
    if workspace.exists():
        if source == "unavailable":
            source = worktrees.git(workspace, "rev-parse", "HEAD").decode().strip()
        worktrees.snapshot(workspace, source, evidence)
    commands = []
    for argv_path in sorted(evidence.glob("command-*.json")):
        if argv_path.name.endswith(".result.json"):
            continue
        stem = argv_path.stem
        saved = evidence / (stem + ".result.json")
        if saved.exists():
            commands.append(CommandRecord.model_validate_json(saved.read_text()))
        else:
            streams = [evidence / (stem + suffix) for suffix in [".stdout", ".stderr"]]
            commands.append(
                CommandRecord(
                    argv=json.loads(argv_path.read_text()),
                    exit_code=None,
                    stdout=streams[0].read_text(errors="replace") if streams[0].exists() else "",
                    stderr=streams[1].read_text(errors="replace") if streams[1].exists() else "",
                )
            )
    (evidence / "stdout.log").write_text("".join(c.stdout for c in commands))
    (evidence / "stderr.log").write_text("".join(c.stderr for c in commands))
    artifacts = []
    diagnostics = workspace / ".argos-coding"
    names = [a.path for a in spec.artifact_requirements if a.producer != "host"]
    if diagnostics.is_dir() and not diagnostics.is_symlink():
        names += [
            str(path.relative_to(workspace)) for path in diagnostics.rglob("*") if path.is_file()
        ]
    for name in names:
        try:
            path = safe_file(workspace, name)
        except ValueError:
            continue
        target = evidence / "recovered-artifacts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        artifacts.append(str(target))
    result = ExperimentResult(
        experiment_id=spec.experiment_id,
        run_id=run_id,
        status="failed",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        worktree=str(workspace),
        diff_path=str(evidence / "code.diff"),
        stdout_path=str(evidence / "stdout.log"),
        stderr_path=str(evidence / "stderr.log"),
        source_commit=source,
        resulting_commit=None,
        configuration=config,
        commands=commands,
        artifacts=artifacts,
        failure=ExecutionFailure(kind="implementation_failure", message=reason),
    )
    temporary = evidence / "result.json.tmp"
    temporary.write_text(result.model_dump_json(indent=2))
    temporary.replace(evidence / "result.json")
    return result
