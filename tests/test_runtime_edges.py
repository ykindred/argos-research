"""Crash, resource, CLI/backend and failure boundaries of the complete runtime."""

import asyncio
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_orchestrator import DEMO_PATH, demo, enqueue, run
from test_orchestrator import setup as _setup

from argos import models as m
from argos.backends import LLMRequest
from argos.backends.command import CommandCodingBackend, CommandLLMBackend
from argos.cli import main
from argos.core.host import build_runtime, refresh_baseline
from argos.execution.process import process_identity, recover_processes
from argos.research import TaskRecordingError
from argos.state import StateError

setup = _setup


def test_command_adapter_uses_stdin_schema_and_preserves_diagnostics(tmp_path):
    backend = CommandLLMBackend(
        [
            sys.executable,
            "-c",
            "import json,sys; r=json.load(sys.stdin); print(json.dumps({'role':r['role']}))",
        ],
        tmp_path / "llm",
    )
    request = LLMRequest(
        task_id=uuid4(), role="manager", system_prompt="test", context_json="{}", output_schema={}
    )
    assert json.loads(asyncio.run(backend.complete(request))) == {"role": "manager"}
    assert list((tmp_path / "llm").rglob("request.json"))
    assert list((tmp_path / "llm").rglob("stderr.log"))
    assert not list((tmp_path / "llm").rglob("*.process.json"))


@pytest.mark.parametrize("argv", [[], "echo", [None], [""]])
def test_command_adapter_rejects_implicit_shell_or_invalid_argv(tmp_path, argv):
    with pytest.raises(ValueError):
        CommandLLMBackend(argv, tmp_path)


def test_command_coding_adapter_integrates_without_control_flow_changes(setup):
    store, project, storage, backend, coding, runtime = setup
    source = (
        """import json,sys
from pathlib import Path
r=json.load(sys.stdin); t=r['task']
assert 'Deterministic' in r['system_prompt']
Path('algorithm.py').write_text(%r)
p=Path('.argos-coding'); (p/'adapter.log').write_text('implemented')
(p/'adapter.diff').write_text('host computes authoritative diff')
print(json.dumps(dict(task_id=t['task_id'], experiment_id=t['experiment_id'],
 status='implemented',diff_path=str(p/'adapter.diff'),log_paths=[str(p/'adapter.log')])))
"""
        % demo.OPTIMIZED
    )
    runtime.executor.backend = CommandCodingBackend([sys.executable, "-c", source])
    run(runtime)
    assert runtime.project.status == "completed"
    assert len(store.list(m.Observation, project_id=project.id)) == 1
    result = store.list(m.Run, project_id=project.id)[0].result
    assert any("implemented" in Path(p).read_text() for p in result.artifacts if p.endswith(".log"))


def test_cli_run_with_stateless_offline_command_and_saved_files(setup, capsys, monkeypatch):
    store, project, storage, backend, coding, runtime = setup
    from argos.backends import command as command_module

    original = command_module.CommandLLMBackend
    configured_timeouts = []

    def configured_backend(*args, **kwargs):
        configured_timeouts.append(kwargs.get("timeout"))
        return original(*args, **kwargs)

    monkeypatch.setattr(command_module, "CommandLLMBackend", configured_backend)
    asyncio.run(refresh_baseline(store, project, storage, rationale="Human baseline"))
    files = storage / "files.json"
    files.write_text(json.dumps({"algorithm.py": demo.OPTIMIZED}))
    command = json.dumps([sys.executable, str(DEMO_PATH.with_name("adapter.py"))])
    assert (
        main(
            [
                "--project",
                str(storage.parent),
                "run",
                "--backend-command",
                command,
                "--fake-files",
                str(files),
            ]
        )
        == 0
    )
    assert store.get(m.Project, project.id).status == "completed"
    assert configured_timeouts == [project.config.resource_limits.timeout_seconds]
    assert len(store.reviews(project.id)) == 1
    capsys.readouterr()


def test_durable_outcome_write_failure_stops_without_publishing_actions(setup):
    store, project, storage, backend, coding, runtime = setup
    original = store.runtime_put

    def fail(project_id, key, payload):
        if key.startswith("outcome:"):
            raise sqlite3.OperationalError("disk full")
        return original(project_id, key, payload)

    store.runtime_put = fail
    with pytest.raises(TaskRecordingError):
        run(runtime)
    assert not store.list(m.Subproblem, project_id=project.id)
    assert store.list(m.Task, project_id=project.id)[0].status == "running"
    assert store.runtime_get(project.id, "cursor")["operation"]["kind"] == "manager"
    store.runtime_put = original
    restarted = build_runtime(store, project, storage, backend, coding)
    run(restarted)
    assert restarted.project.status == "completed"
    assert len(store.list(m.Task, project_id=project.id, status="failed")) == 1


def test_saved_critic_response_survives_crash_before_review_insertion(setup):
    store, project, storage, backend, coding, runtime = setup
    original = runtime.reviews.record
    runtime.reviews.record = lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    with pytest.raises(RuntimeError):
        run(runtime)
    assert not store.reviews(project.id)
    assert len([r for r in backend.requests if r.role == "critic"]) == 1
    restarted = build_runtime(store, project, storage, backend, coding)
    restarted.reviews.record = original
    run(restarted)
    assert len(store.reviews(project.id)) == 1
    assert len([r for r in backend.requests if r.role == "critic"]) == 1


def test_evaluation_manifest_recovery_does_not_repeat_measurement(setup, monkeypatch):
    store, project, storage, backend, coding, runtime = setup
    import argos.core.orchestrator as module

    original = module.record_evaluation

    def crash(*args):
        raise RuntimeError("Crash after evaluation manifest")

    monkeypatch.setattr(module, "record_evaluation", crash)
    with pytest.raises(RuntimeError):
        run(runtime)
    assert not store.list(m.Observation, project_id=project.id)
    assert len(list((storage / "evaluation").rglob("evaluation.json"))) == 1
    monkeypatch.setattr(module, "record_evaluation", original)
    restarted = build_runtime(store, project, storage, backend, coding)
    run(restarted)
    assert len(store.list(m.Observation, project_id=project.id)) == 1
    assert len(list((storage / "evaluation").rglob("evaluation.json"))) == 1


def test_truncated_execution_manifest_preserves_evidence_and_fails_without_replay(setup):
    store, project, storage, backend, coding, runtime = setup

    def crash(result):
        raise RuntimeError("Crash before execution state commit")

    runtime._execution_result = crash
    with pytest.raises(RuntimeError):
        run(runtime)
    manifest = next((storage / "execution").rglob("result.json"))
    manifest.write_text('{"status":')
    restarted = build_runtime(store, project, storage, backend, coding)
    run(restarted)
    assert restarted.project.status == "completed"
    assert len(coding.calls) == 1
    result = store.list(m.Run, project_id=project.id)[0].result
    assert result.status == "failed"
    assert "manifest" in result.failure.message.lower()
    assert next(manifest.parent.glob("result.invalid-*.json")).read_text() == '{"status":'
    assert not store.list(m.Observation, project_id=project.id)


def test_truncated_baseline_execution_manifest_requires_explicit_new_measurement(
    setup, monkeypatch
):
    from argos.evaluation import BaselineRunner

    store, project, storage, backend, coding, runtime = setup
    original = BaselineRunner.execute

    async def crash(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("Crash before baseline state commit")

    monkeypatch.setattr(BaselineRunner, "execute", crash)
    with pytest.raises(RuntimeError):
        asyncio.run(refresh_baseline(store, project, storage, rationale="Human baseline"))
    manifest = next((storage / "baselines").rglob("result.json"))
    manifest.write_text('{"status":')
    monkeypatch.setattr(BaselineRunner, "execute", original)
    with pytest.raises(StateError, match="Baseline failed"):
        asyncio.run(refresh_baseline(store, project, storage, rationale="Human recovery"))
    runs = store.list(m.Run, project_id=project.id)
    assert len(runs) == 1 and runs[0].status == "failed"
    assert len(runs[0].result.commands) == 3
    assert runtime.project.baseline_id is None
    assert not store.runtime_get(project.id, "baseline_cursor")
    assert next(manifest.parent.glob("result.invalid-*.json")).read_text() == '{"status":'


def test_cancelled_execution_is_persisted_and_never_automatically_retried(setup):
    store, project, storage, backend, coding, runtime = setup
    run(runtime, 9)
    original = coding.run

    async def cancel(*args):
        raise asyncio.CancelledError()

    coding.run = cancel
    with pytest.raises(asyncio.CancelledError):
        run(runtime)
    coding.run = original
    restarted = build_runtime(store, project, storage, backend, coding)
    run(restarted)
    assert not coding.calls
    result = store.list(m.Run, project_id=project.id)[0]
    assert result.status == "failed"
    assert "Cancelled" in result.result.failure.message
    assert not store.list(m.Observation, project_id=project.id)


def test_actual_killed_runner_recovers_partial_execution_diff_logs_and_child(setup):
    store, project, storage, backend, coding, runtime = setup
    run(runtime, 9)
    # A separate interpreter performs the actual execution, then is killed while a
    # command process is active. Its durable cursor and raw evidence drive recovery.
    code = """import asyncio,sys
from pathlib import Path
from uuid import UUID
from argos.state import StateStore
from argos.models import Project
from argos.core.host import build_runtime
from argos.backends import FakeLLMBackend
from argos.backends.coding import FakeCodingBackend
from argos.execution.process import ProcessRunner
class SlowCoding(FakeCodingBackend):
 async def run(self, workspace, task, timeout):
  await super().run(workspace, task, timeout)
  d=workspace/'.argos-coding'
  argv=[sys.executable,'-c',"import time; print('partial',flush=True); time.sleep(60)"]
  await ProcessRunner().run(argv,workspace,60,d/'child.out',d/'child.err')
with StateStore(sys.argv[1]) as store:
 p=store.get(Project,UUID(sys.argv[2]))
 backend=SlowCoding({'algorithm.py': 'partial = True\\n'})
 r=build_runtime(store,p,Path(sys.argv[1]).parent,FakeLLMBackend({}),backend)
 asyncio.run(r.run())
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", code, str(storage / "state.sqlite"), str(project.id)]
    )
    child = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            markers = list((storage / "execution").rglob("*.process.json"))
            if markers and markers[0].with_name("child.out").read_text().strip() == "partial":
                child = json.loads(markers[0].read_text())["pid"]
                break
            time.sleep(0.02)
        assert child is not None
        worker.kill()
        worker.wait(timeout=5)
        restarted = build_runtime(store, project, storage, backend, coding)
        run(restarted)
        result = store.list(m.Run, project_id=project.id)[0].result
        assert result.status == "failed"
        assert "partial = True" in Path(result.diff_path).read_text()
        assert result.source_commit != "unavailable"
        assert Path(result.worktree, ".argos-coding/child.out").read_text().strip() == "partial"
        assert not coding.calls
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            stat = Path(f"/proc/{child}/stat")
            if not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.02)
        else:
            pytest.fail("Orphan command was not terminated")
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if child and process_identity(child):
            try:
                os.killpg(child, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_recovery_never_signals_reused_process_identity(tmp_path, monkeypatch):
    marker = tmp_path / "stdout.process.json"
    marker.write_text(json.dumps({"pid": os.getpid(), "identity": ["other-boot", "1"]}))

    def forbidden(*args):
        pytest.fail("Must not signal an unrelated PID")

    monkeypatch.setattr(os, "killpg", forbidden)
    recover_processes(tmp_path)
    assert not marker.exists()


def test_failed_baseline_retains_run_and_does_not_replace_pointer(setup):
    store, project, storage, backend, coding, runtime = setup
    # Baseline runner gets a trusted config for this deliberately broken test command.
    project.config.test_command = [sys.executable, "-c", "raise SystemExit(2)"]
    with pytest.raises(StateError, match="Baseline failed"):
        asyncio.run(refresh_baseline(store, project, storage, rationale="human test"))
    assert runtime.project.baseline_id is None
    results = store.list(m.Run, project_id=project.id)
    assert len(results) == 1 and results[0].status == "failed"
    assert Path(results[0].result.stderr_path).exists()


def test_generic_human_gate_and_protected_settings_are_not_automatically_approved(setup):
    store, project, storage, backend, coding, runtime = setup
    enqueue(
        runtime,
        dict(
            action_type="propose_protected_change",
            target="evaluator",
            proposed_change="Replace correctness tests",
        ),
    )
    run(runtime)
    gate = runtime.state.gate
    with pytest.raises(StateError):
        runtime.answer(gate.id, "approve", approve=True)
    assert runtime.project.config == project.config
    runtime.answer(gate.id, "Keep current evaluator")
    assert runtime.project.config == project.config
    enqueue(
        runtime,
        dict(action_type="pause_for_human", question_for_human="Continue?", options=["yes"]),
    )
    run(runtime)
    runtime.answer(runtime.state.gate.id, "yes")
    assert runtime.project.status == "active"


def test_missing_critic_evidence_is_a_task_failure_not_a_crash(setup):
    store, project, storage, backend, coding, runtime = setup
    # Stop right after the claim is formed and remove only the test's diff artifact.
    run(runtime, 13)
    claim = store.list(m.Claim, project_id=project.id)[0]
    Path(store.claim_provenance(claim.id)[0].run.result.diff_path).unlink()
    enqueue(runtime, dict(action_type="request_critic_review", claim_id=claim.id))
    run(runtime, 1)
    assert not store.reviews(project.id)
    failures = store.list(m.Task, project_id=project.id, status="failed")
    assert "evidence unavailable" in failures[0].failure


@pytest.mark.parametrize("field", ["baseline_id", "protocol_name"])
def test_mismatched_evaluator_context_is_failure_and_returns_to_manager(setup, field):
    store, project, storage, backend, coding, runtime = setup
    original = runtime.evaluator.evaluate

    async def mismatched(*args, **kwargs):
        result = await original(*args, **kwargs)
        # Simulate a pluggable evaluator returning a valid object for the wrong protocol.
        return result.model_copy(update={field: uuid4() if field == "baseline_id" else "wrong"})

    runtime.evaluator.evaluate = mismatched
    run(runtime)
    assert runtime.project.status == "completed"
    result = store.list(m.Run, project_id=project.id)[0]
    assert result.evaluation.status == "invalid"
    assert not store.list(m.Observation, project_id=project.id)
    assert store.list(m.Task, project_id=project.id, status="failed")[0].kind == "evaluation"
    assert all(h.status == "proposed" for h in store.list(m.Hypothesis, project_id=project.id))


@pytest.mark.parametrize("baseline", [False, True])
def test_restart_before_manifest_publish_preserves_evidence_without_replay(
    setup, monkeypatch, baseline
):
    store, project, storage, backend, coding, runtime = setup
    original = Path.replace
    filename = "result.json.tmp" if baseline else "evaluation.json.tmp"

    def interrupt(path, target):
        if path.name == filename:
            raise SystemExit("Process stopped before atomic publication")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", interrupt)
    with pytest.raises(SystemExit):
        if baseline:
            asyncio.run(refresh_baseline(store, project, storage, rationale="Human baseline"))
        else:
            run(runtime)
    monkeypatch.setattr(Path, "replace", original)
    if baseline:
        assert not list((storage / "baselines").rglob("result.json"))
        with pytest.raises(StateError, match="Baseline failed"):
            asyncio.run(refresh_baseline(store, project, storage, rationale="Human recovery"))
        result = store.list(m.Run, project_id=project.id)[0].result
        assert result.status == "failed"
        assert len(result.commands) == 3
        assert all(c.exit_code == 0 for c in result.commands)
        assert runtime.project.baseline_id is None
        assert not store.runtime_get(project.id, "baseline_cursor")
    else:
        assert not list((storage / "evaluation").rglob("evaluation.json"))
        restarted = build_runtime(store, project, storage, backend, coding)
        run(restarted)
        assert restarted.project.status == "completed"
        assert len(coding.calls) == 1
        assert len(list((storage / "evaluation").rglob("input.json"))) == 1
        assert store.list(m.Run, project_id=project.id)[0].evaluation.status == "invalid"
        assert not store.list(m.Observation, project_id=project.id)


def test_restart_with_truncated_evaluation_manifest_records_invalid_result(setup, monkeypatch):
    store, project, storage, backend, coding, runtime = setup
    import argos.core.orchestrator as module

    original = module.record_evaluation

    def crash(*args):
        raise RuntimeError("Crash before state commit")

    monkeypatch.setattr(module, "record_evaluation", crash)
    with pytest.raises(RuntimeError):
        run(runtime)
    manifest = next((storage / "evaluation").rglob("evaluation.json"))
    manifest.write_text('{"status":')
    monkeypatch.setattr(module, "record_evaluation", original)
    restarted = build_runtime(store, project, storage, backend, coding)
    run(restarted)
    assert restarted.project.status == "completed"
    assert manifest.read_text() == '{"status":'  # Retain the failed evidence for inspection.
    assert store.list(m.Run, project_id=project.id)[0].evaluation.status == "invalid"
    assert not store.list(m.Observation, project_id=project.id)


def test_failed_experiment_can_be_followed_by_explicit_successful_attempt(setup):
    store, project, storage, backend, coding, runtime = setup
    original_complete = backend.complete
    original_coding = coding.run
    attempts = 0
    proposed_retry = False

    async def fail_once(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("First implementation failed")
        return await original_coding(*args)

    async def propose_retry(request):
        nonlocal proposed_retry
        if request.role == "manager":
            context = json.loads(request.context_json)
            if context["frontier"]["failed_runs"] and not proposed_retry:
                proposed_retry = True
                previous = store.list(m.Experiment, project_id=project.id)[0]
                spec = previous.spec.model_copy(update={"experiment_id": uuid4()})
                return json.dumps(
                    {
                        "summary": "Explicitly retry implementation with the same scientific test",
                        "actions": [
                            {
                                "project_id": str(project.id),
                                "rationale": "Failure is engineering evidence, not falsification",
                                "action": {
                                    "action_type": "propose_experiment",
                                    "spec": spec.model_dump(mode="json"),
                                },
                            }
                        ],
                    }
                )
        return await original_complete(request)

    backend.complete = propose_retry
    coding.run = fail_once
    run(runtime)
    assert runtime.project.status == "completed"
    assert attempts == 2
    results = store.list(m.Run, project_id=project.id)
    assert {r.status for r in results} == {"failed", "succeeded"}
    assert len({r.result.worktree for r in results}) == 2
    assert all(Path(r.result.diff_path).exists() for r in results)
    assert len(store.list(m.Observation, project_id=project.id)) == 1
    assert len(store.reviews(project.id)) == 1
    assert all(h.status == "proposed" for h in store.list(m.Hypothesis, project_id=project.id))
