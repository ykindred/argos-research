"""Real temporary Git repositories and subprocesses; no services or credentials."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from argos.backends.coding import FakeCodingBackend
from argos.execution import ExperimentAgent, ProcessRunner, WorktreeManager
from argos.protocols import ExperimentResult, ExperimentSpec, ProjectConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "protocols"


def git(repo, *args):
    return (
        subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE)
        .decode()
        .strip()
    )


def py(script):
    return [sys.executable, "-c", script]


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("print('original')\n")
    (repo / "evaluate.py").write_text("print('protected evaluator')\n")
    (repo / ".gitignore").write_text("*.cache\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "fixture")
    project = ProjectConfig.model_validate_json((EXAMPLES / "project_config.json").read_text())
    project.source_repository = str(repo)
    spec = ExperimentSpec.model_validate_json((EXAMPLES / "experiment_spec.json").read_text())
    spec.build_steps = [py("print('build')")]
    spec.test_steps = [py("print('test')")]
    spec.run_steps = [
        py("import sys; print('negative measurement'); print('diagnostic', file=sys.stderr)")
    ]
    return repo, project, spec, tmp_path / "runs"


def execute(setup, backend=None, **kwargs):
    repo, project, spec, storage = setup
    agent = ExperimentAgent(project, backend or FakeCodingBackend(), storage, **kwargs)
    result = asyncio.run(agent.execute(spec))
    assert (
        ExperimentResult.model_validate_json(
            (Path(result.diff_path).parent / "result.json").read_text()
        )
        == result
    )
    assert git(repo, "status", "--porcelain") == ""
    assert (repo / "src/main.py").read_text() == "print('original')\n"
    return agent, result


def test_success_records_revision_commands_artifacts_and_cleanup(setup):
    repo, _, spec, _ = setup
    spec.required_artifacts = ["output.json"]
    spec.run_steps += [
        py("from pathlib import Path; Path('output.json').write_text('{\"metric\": -1}')")
    ]
    backend = FakeCodingBackend({"src/main.py": "print('modified')\n", "src/new.py": "# added\n"})
    source = git(repo, "rev-parse", "HEAD")
    agent, result = execute(setup, backend)
    assert result.status == "succeeded"
    assert result.source_commit == source
    assert result.resulting_commit != source
    assert git(repo, "show", f"{result.resulting_commit}:src/main.py") == "print('modified')"
    assert git(repo, "rev-parse", "HEAD") == source
    assert len(backend.calls) == 1
    assert backend.calls[0].project_scope == setup[1].scope
    assert len(result.commands) == 4
    assert "negative measurement" in Path(result.stdout_path).read_text()
    assert "diagnostic" in Path(result.stderr_path).read_text()
    assert "src/new.py" in Path(result.diff_path).read_text()
    artifact = Path(result.artifacts[-1])
    assert json.loads(artifact.read_text()) == {"metric": -1}
    evidence = Path(result.diff_path).parent
    agent.worktrees.cleanup(Path(result.worktree), evidence)
    assert not Path(result.worktree).exists()
    assert artifact.exists() and Path(result.diff_path).exists()
    assert (evidence / "coding-0.log").exists()


def test_no_change_and_repeated_attempts_are_isolated(setup):
    _, first = execute(setup)
    _, second = execute(setup)
    assert first.resulting_commit == first.source_commit
    assert first.run_id != second.run_id
    assert first.worktree != second.worktree


@pytest.mark.parametrize(
    "phase,kind", [("build", "build_failure"), ("test", "test_failure"), ("run", "runtime_crash")]
)
def test_nonzero_exit_short_circuits_and_preserves_failure(setup, phase, kind):
    spec = setup[2]
    setattr(spec, phase + "_steps", [py("import sys; print('failure'); sys.exit(7)")])
    agent, result = execute(setup, FakeCodingBackend({"src/main.py": "# implementation\n"}))
    assert result.status == "failed" and result.failure.kind == kind
    assert result.commands[-1].exit_code == 7
    assert "failure" in Path(result.stdout_path).read_text()
    assert "implementation" in Path(result.diff_path).read_text()
    assert len(agent.backend.calls) == 1  # no repair without an explicit new decision
    agent.worktrees.cleanup(Path(result.worktree), Path(result.diff_path).parent)
    assert Path(result.diff_path).exists()


@pytest.mark.parametrize("name", ["evaluate.py", "outside.py", "src/new.cache"])
def test_scope_enforced_for_protected_untracked_and_ignored_files(setup, name):
    if name == "src/new.cache":
        setup[1].scope.protected_paths.append("*.cache")
    _, result = execute(setup, FakeCodingBackend({name: "forbidden modification\n"}))
    assert result.failure.kind == "invalid_modification"
    assert not result.commands
    assert "forbidden modification" in Path(result.diff_path).read_text()


def test_spec_cannot_relax_project_policy_or_replace_evaluator(setup):
    spec = setup[2]
    spec.scope.editable_paths = ["**"]
    spec.scope.protected_paths = []
    _, result = execute(setup, FakeCodingBackend({"evaluate.py": "bad"}))
    assert result.failure.kind == "invalid_modification"
    spec.evaluation_protocol = spec.evaluation_protocol.model_copy(update={"name": "replacement"})
    agent, result = execute(setup)
    assert result.failure.kind == "invalid_modification"
    assert not agent.backend.calls


@pytest.mark.parametrize("command", [["/nonexistent-argos-command"], ["bad\x00command"]])
def test_invalid_command(setup, command):
    setup[2].run_steps = [command]
    _, result = execute(setup)
    assert result.failure.kind == "invalid_command"
    assert result.commands[-1].exit_code is None
    assert Path(result.stderr_path).read_text()


def test_missing_artifact(setup):
    setup[2].required_artifacts = ["missing.json"]
    _, result = execute(setup)
    assert result.failure.kind == "missing_artifact"


@pytest.mark.parametrize("name", ["../escape", "/absolute", ".git/config"])
def test_unsafe_artifact_requests_never_invoke_backend(setup, name):
    setup[2].required_artifacts = [name]
    agent, result = execute(setup)
    assert result.status == "failed"
    assert not agent.backend.calls


def test_runtime_source_mutation_is_rejected(setup):
    setup[2].run_steps = [
        py("from pathlib import Path; Path('evaluate.py').write_text('tampered')")
    ]
    _, result = execute(setup)
    assert result.failure.kind == "invalid_modification"
    assert "tampered" in Path(result.diff_path).read_text()


def test_backend_exception_is_structured_and_partial_diff_survives(setup):
    class Broken:
        async def run(self, workspace, task, timeout):
            (workspace / "src/main.py").write_text("partial implementation")
            raise RuntimeError("backend failed")

    _, result = execute(setup, Broken())
    assert result.failure.kind == "implementation_failure"
    assert "partial implementation" in Path(result.diff_path).read_text()


def test_backend_invalid_identity_is_rejected(setup):
    class Wrong(FakeCodingBackend):
        async def run(self, workspace, task, timeout):
            from uuid import uuid4

            result = await super().run(workspace, task, timeout)
            result.task_id = uuid4()
            return result

    _, result = execute(setup, Wrong())
    assert result.failure.kind == "implementation_failure"
    assert not result.commands


def test_mutated_spec_is_revalidated_before_execution(setup):
    setup[2].run_steps.append([])
    with pytest.raises(ValidationError):
        execute(setup)
    assert not setup[3].exists()


def test_command_timeout_kills_descendant_and_retains_output(setup):
    setup[2].run_steps = [
        py(
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c', 'import time; time.sleep(60)']); "
            "print(p.pid, flush=True); time.sleep(60)"
        )
    ]
    _, result = execute(setup, command_timeout_seconds=0.3)
    assert result.failure.kind == "timeout"
    pid = int(result.commands[-1].stdout)
    proc = Path(f"/proc/{pid}/stat")
    assert not proc.exists() or proc.read_text().split()[2] == "Z"
    assert result.commands[-1].exit_code != 0


def test_total_timeout_cancels_backend_and_retains_failure(setup):
    setup[2].resource_limits.timeout_seconds = 1

    class Slow:
        cancelled = False

        async def run(self, workspace, task, timeout):
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled = True

    backend = Slow()
    _, result = execute(setup, backend)
    assert backend.cancelled
    assert result.failure.kind == "timeout"


def test_total_timeout_during_command_records_attempt(setup):
    setup[2].resource_limits.timeout_seconds = 1
    setup[2].run_steps = [py("import time; print('before timeout', flush=True); time.sleep(60)")]
    _, result = execute(setup)
    assert result.failure.kind == "timeout"
    assert "before timeout" in result.commands[-1].stdout
    assert result.commands[-1].exit_code == -9


def test_storage_inside_canonical_checkout_rejected(setup):
    with pytest.raises(ValueError, match="outside"):
        ExperimentAgent(setup[1], FakeCodingBackend(), setup[0] / "runs")


def test_cleanup_requires_persisted_manifest(setup):
    with pytest.raises(FileNotFoundError):
        WorktreeManager(setup[0]).cleanup(setup[3] / "worktree", setup[3] / "evidence")


def test_symlink_code_is_rejected_without_reading_target(setup):
    class Symlink:
        async def run(self, workspace, task, timeout):
            (workspace / "src/link").symlink_to(setup[0] / "evaluate.py")
            return await FakeCodingBackend().run(workspace, task, timeout)

    _, result = execute(setup, Symlink())
    assert result.failure.kind == "invalid_modification"


def test_coding_queue_is_bounded(setup):
    class Counting(FakeCodingBackend):
        active = 0
        peak = 0

        async def run(self, workspace, task, timeout):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.05)
                return await super().run(workspace, task, timeout)
            finally:
                self.active -= 1

    backend = Counting()
    agent = ExperimentAgent(setup[1], backend, setup[3])

    async def run():
        return await asyncio.gather(*(agent.execute(setup[2]) for _ in range(3)))

    results = asyncio.run(run())
    assert all(result.status == "succeeded" for result in results)
    assert backend.peak == 1


def test_process_cancellation_kills_tree(tmp_path):
    async def run():
        runner = ProcessRunner()
        task = asyncio.create_task(
            runner.run(
                py("import os,time; print(os.getpid(),flush=True); time.sleep(60)"),
                tmp_path,
                60,
                tmp_path / "out",
                tmp_path / "err",
            )
        )
        for _ in range(100):
            if (tmp_path / "out").exists() and (tmp_path / "out").read_text():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    pid = int((tmp_path / "out").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_failed_run_keeps_partial_artifact_and_allows_next_attempt(setup):
    setup[2].required_artifacts = ["partial.txt"]
    setup[2].run_steps = [
        py("from pathlib import Path; Path('partial.txt').write_text('partial evidence'); exit(2)")
    ]
    agent, failed = execute(setup)
    assert failed.failure.kind == "runtime_crash"
    artifact = Path(failed.artifacts[-1])
    assert artifact.read_text() == "partial evidence"
    agent.worktrees.cleanup(Path(failed.worktree), Path(failed.diff_path).parent)
    assert artifact.read_text() == "partial evidence"
    setup[2].required_artifacts = []
    setup[2].run_steps = [py("print('next attempt')")]
    result = asyncio.run(agent.execute(setup[2]))
    assert result.status == "succeeded"


@pytest.mark.parametrize(
    "status,kind", [("failed", "implementation_failure"), ("timeout", "timeout")]
)
def test_explicit_coding_failure_is_not_run_or_retried(setup, status, kind):
    from argos.common import ExecutionFailure

    class Failed(FakeCodingBackend):
        async def run(self, workspace, task, timeout):
            result = await super().run(workspace, task, timeout)
            return result.model_copy(
                update={
                    "status": status,
                    "failure": ExecutionFailure(
                        kind=kind, message="implementation could not finish"
                    ),
                }
            )

    backend = Failed()
    _, result = execute(setup, backend)
    assert result.failure.kind == kind
    assert not result.commands
    assert len(backend.calls) == 1
    assert Path(result.artifacts[0]).is_file()


def test_deleted_and_binary_files_are_retained_in_patch(setup):
    class Changes(FakeCodingBackend):
        async def run(self, workspace, task, timeout):
            (workspace / "src/main.py").unlink()
            (workspace / "src/binary.bin").write_bytes(b"\x00\xff\x00")
            return await super().run(workspace, task, timeout)

    _, result = execute(setup, Changes())
    assert result.status == "succeeded"
    patch = Path(result.diff_path).read_text()
    assert "deleted file mode" in patch
    assert "GIT binary patch" in patch


@pytest.mark.parametrize("resource", ["cpu", "gpu"])
def test_execution_resource_queue_is_bounded(setup, resource):
    class ObservedRunner(ProcessRunner):
        active = 0
        peak = 0

        async def run(self, *args, **kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                return await super().run(*args, **kwargs)
            finally:
                self.active -= 1

    setup[2].resource_class = resource
    setup[2].build_steps = []
    setup[2].test_steps = []
    setup[2].run_steps = [py("import time; time.sleep(0.1)")]
    agent = ExperimentAgent(setup[1], FakeCodingBackend(), setup[3])
    agent.process = ObservedRunner()

    async def run():
        return await asyncio.gather(*(agent.execute(setup[2]) for _ in range(3)))

    assert all(r.status == "succeeded" for r in asyncio.run(run()))
    assert agent.process.peak == 1


def test_explicit_cancellation_persists_result_before_propagating(setup):
    class Waiting(FakeCodingBackend):
        async def run(self, workspace, task, timeout):
            await super().run(workspace, task, timeout)
            await asyncio.sleep(60)

    agent = ExperimentAgent(setup[1], Waiting(), setup[3])

    async def run():
        task = asyncio.create_task(agent.execute(setup[2]))
        for _ in range(100):
            if list(setup[3].glob("*/*/worktree/.argos-coding/coding.log")):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    manifests = list(setup[3].glob("*/*/evidence/result.json"))
    assert len(manifests) == 1
    result = ExperimentResult.model_validate_json(manifests[0].read_text())
    assert result.status == "failed"
    assert "Cancelled" in result.failure.message
    assert any("backend-diagnostics" in p for p in result.artifacts)


def test_cleanup_refuses_incomplete_preservation(setup):
    agent, result = execute(setup)
    evidence = Path(result.diff_path).parent
    (evidence / "preservation-error.log").write_text("disk failure")
    with pytest.raises(ValueError, match="incomplete"):
        agent.worktrees.cleanup(Path(result.worktree), evidence)
    assert Path(result.worktree).exists()


def test_owned_artifacts_reach_coding_and_host_records_actual_revision(setup):
    from argos.protocols import ArtifactRequirement

    setup[2].required_artifacts = [
        ArtifactRequirement(path=".argos-coding/implementation.txt", producer="coding"),
        ArtifactRequirement(path="code.diff", producer="host"),
        ArtifactRequirement(path="revision.json", producer="host"),
    ]
    backend = FakeCodingBackend({".argos-coding/implementation.txt": "Implemented nothing"})
    _, result = execute(setup, backend)
    assert result.status == "succeeded"
    assert [a.path for a in backend.calls[0].required_artifacts] == [
        ".argos-coding/implementation.txt"
    ]
    revision = json.loads((Path(result.diff_path).parent / "revision.json").read_text())
    assert revision["resulting_commit"] == result.resulting_commit
    assert result.diff_path in result.artifacts


@pytest.mark.parametrize("name", ["evaluate.py", "outside.txt"])
def test_coding_artifact_scope_rejected_before_backend(setup, name):
    setup[2].required_artifacts = [{"path": name, "producer": "coding"}]
    agent, result = execute(setup)
    assert result.failure.kind == "invalid_modification"
    assert not agent.backend.calls


def test_missing_owned_artifact_identifies_responsible_producer(setup):
    setup[2].required_artifacts = [{"path": ".argos-coding/missing.txt", "producer": "coding"}]
    _, result = execute(setup)
    assert result.failure.kind == "missing_artifact"
    assert "coding producer" in result.failure.message


def test_source_evidence_is_scoped_bounded_and_from_immutable_git(setup):
    from argos.execution.evidence import source_evidence

    repo, project, _, _ = setup
    manager = WorktreeManager(repo)
    revision = manager.source_commit()
    (repo / "src/main.py").write_text("UNCOMMITTED PRIVATE CHANGE")
    (repo / "secret.txt").write_text("not in scope")
    evidence = source_evidence(manager, repo, revision, project.scope)
    files = {f["path"]: f for f in evidence["files"]}
    assert "secret.txt" not in files
    assert files["src/main.py"]["content"] == "print('original')\n"
    assert files["evaluate.py"]["role"] == "protected"
    bounded = source_evidence(manager, repo, revision, project.scope, budget=0)
    assert all("content" not in f and f["git_blob"] for f in bounded["files"])
    limited = source_evidence(manager, repo, revision, project.scope, max_files=1)
    assert len(limited["files"]) == 1 and limited["omitted_files"] > 0


def test_successful_execution_persists_candidate_source_evidence(setup):
    _, result = execute(setup, FakeCodingBackend({"src/main.py": "print('candidate')\n"}))
    assert result.source_evidence["revision"] == result.resulting_commit
    files = {f["path"]: f for f in result.source_evidence["files"]}
    assert files["src/main.py"]["content"] == "print('candidate')\n"
    assert files["evaluate.py"]["content"] == "print('protected evaluator')\n"
