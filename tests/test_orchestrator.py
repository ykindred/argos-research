"""Runtime integration: real temporary Git worktrees, SQLite, and deterministic roles."""

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from argos import models as m
from argos.backends.coding import FakeCodingBackend
from argos.cli import main
from argos.core.host import build_runtime, refresh_baseline, runner_lock
from argos.core.orchestrator import now
from argos.evaluation import FakeEvaluator
from argos.protocols import ManagerAction
from argos.state import StateError, StateStore

DEMO_PATH = Path(__file__).resolve().parents[1] / "examples/synthetic/demo.py"
module_spec = importlib.util.spec_from_file_location("synthetic", DEMO_PATH)
demo = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(demo)


@pytest.fixture
def setup(tmp_path):
    config = demo.prepare(tmp_path / "project")
    storage = tmp_path / "project/.argos"
    storage.mkdir()
    store = StateStore(storage / "state.sqlite")
    project = store.create(m.Project(id=uuid4(), created_at=now(), name="test", config=config))
    project = store.approve_main_question(
        project.id, config.main_research_question, rationale="test human"
    )
    backend = demo.SyntheticLLM()
    coding = FakeCodingBackend({"algorithm.py": demo.OPTIMIZED})
    runtime = build_runtime(store, project, storage, backend, coding)
    yield store, project, storage, backend, coding, runtime
    store.close()


def run(runtime, steps=None):
    return asyncio.run(runtime.run(max_transitions=steps))


def enqueue(runtime, *payloads):
    runtime.state.actions = [
        ManagerAction(project_id=runtime.project_id, rationale="Test RM action", action=p)
        for p in payloads
    ]
    runtime._save()


@pytest.mark.parametrize("fake_evaluation", [False, True])
def test_full_loop_provenance_isolation_join_and_review_feedback(setup, fake_evaluation):
    store, project, storage, backend, coding, runtime = setup
    asyncio.run(refresh_baseline(store, project, storage, rationale="Human baseline"))
    if fake_evaluation:
        runtime.evaluator = FakeEvaluator(
            project.config,
            storage / "fake-evaluation",
            json.dumps(
                {
                    "status": "ok",
                    "metrics": {
                        "comparisons": {"value": 800, "direction": "minimize"},
                        "runtime": {"value": 0.01, "unit": "second", "direction": "minimize"},
                        "peak_memory": {"value": 12000, "unit": "byte", "direction": "minimize"},
                    },
                    "constraints": {"correctness": True},
                    "artifacts": [],
                }
            ),
        )
    run(runtime)
    assert runtime.project.status == "completed"
    assert len(coding.calls) == 1
    assert len(store.list(m.ResearchBranch, project_id=project.id)) == 3
    ra = [r for r in backend.requests if r.role == "research_agent"]
    assert len(ra) == 3
    assert {json.loads(r.context_json)["task"]["perspective"] for r in ra} >= {"falsification"}
    assert all("frontier" not in json.loads(r.context_json) for r in ra)
    manager_contexts = [json.loads(r.context_json) for r in backend.requests if r.role == "manager"]
    synthesis = next(c for c in manager_contexts if c["exploration"])
    assert len(synthesis["exploration"]["outcomes"]) == 3
    assert backend.requests.index(
        next(
            r
            for r in backend.requests
            if r.role == "manager" and json.loads(r.context_json)["exploration"]
        )
    ) > max(backend.requests.index(r) for r in ra)
    chain = store.claim_provenance(store.list(m.Claim, project_id=project.id)[0].id)
    assert len(chain) == 1
    row = chain[0]
    assert row.observation.relation == "neutral"
    assert row.hypothesis.status == "proposed"
    assert row.run.result.source_commit != row.run.result.resulting_commit
    assert "set()" in Path(row.run.result.diff_path).read_text()
    assert "seen = []" in (Path(project.config.source_repository) / "algorithm.py").read_text()
    assert row.run.evaluation.comparisons[0].delta < 0
    assert len(store.reviews(project.id)) == 1
    feedback = next(c for c in manager_contexts if c["event"] == "review_recorded")
    assert feedback["frontier"]["reviews"][0]["review"]["requested_checks"]
    critic = next(json.loads(r.context_json) for r in backend.requests if r.role == "critic")
    assert "frontier" not in critic and "decisions" not in critic
    for result in [r.result for r in store.list(m.Run, project_id=project.id)]:
        assert all(
            Path(p).exists() for p in [result.diff_path, result.stdout_path, result.stderr_path]
        )
    # Each observation existed in SQLite before the event was dispatched to RM.
    observation_event = next(c for c in manager_contexts if c["event"] == "observations_recorded")
    assert any(
        o["id"] == str(row.observation.id)
        for o in observation_event["frontier"]["recent_observations"]
    )
    with StateStore(storage / "state.sqlite") as reopened:
        assert reopened.claim_provenance(row.claim.id) == chain
        assert reopened.runtime_get(project.id, "cursor")["cycle"] == 8


def test_pause_reopen_resume_does_not_duplicate_actions(setup):
    store, project, storage, backend, coding, runtime = setup
    run(runtime, 2)  # plan + create subproblem
    runtime.pause()
    calls = len(backend.requests)
    run(runtime)
    assert len(backend.requests) == calls
    with StateStore(storage / "state.sqlite") as reopened:
        resumed = build_runtime(
            reopened, reopened.get(m.Project, project.id), storage, backend, coding
        )
        resumed.resume()
        run(resumed)
        assert len(reopened.list(m.Subproblem, project_id=project.id)) == 1
        assert resumed.project.status == "completed"


@pytest.mark.parametrize(
    "approve,edit", [(False, None), (True, None), (True, "Human edited question")]
)
def test_main_question_gate_reject_accept_edit_and_stale_answers(setup, approve, edit):
    store, project, storage, backend, coding, runtime = setup
    enqueue(
        runtime, dict(action_type="propose_main_question_revision", proposed_question="Proposed Q")
    )
    run(runtime)
    assert runtime.project.config.main_research_question == project.config.main_research_question
    assert runtime.project.status == "paused"
    gate = runtime.state.gate
    with pytest.raises(StateError):
        runtime.resume()
    with pytest.raises(StateError):
        runtime.answer(uuid4(), "stale", approve=True)
    runtime.answer(gate.id, "Explicit human choice", approve=approve, edited_question=edit)
    expected = (edit or "Proposed Q") if approve else project.config.main_research_question
    assert runtime.project.config.main_research_question == expected
    assert runtime.project.status == "active"
    assert runtime.state.event == "human_answered"
    with pytest.raises(StateError):
        runtime.answer(gate.id, "replayed", approve=True)


@pytest.mark.parametrize("failure", ["coding", "test", "invalid_evaluation", "critic", "ra"])
def test_component_failure_is_durable_and_loop_survives(setup, failure):
    store, project, storage, backend, coding, runtime = setup
    original = backend.complete

    async def complete(request):
        if request.role == failure or (failure == "ra" and request.role == "research_agent"):
            return "not json"
        return await original(request)

    backend.complete = complete
    if failure == "coding":

        async def broken(*args):
            raise RuntimeError("implementation failed")

        coding.run = broken
    elif failure == "test":
        coding.files = {"algorithm.py": "def count_distinct(values): return -1, 0\n"}
    elif failure == "invalid_evaluation":
        runtime.evaluator = FakeEvaluator(project.config, storage / "bad-eval", "invalid json")
    run(runtime)
    assert runtime.project.status == "completed"
    assert all(h.status == "proposed" for h in store.list(m.Hypothesis, project_id=project.id))
    if failure in ("coding", "test"):
        results = store.list(m.Run, project_id=project.id)
        assert results[0].status == "failed"
        assert Path(results[0].result.diff_path).exists()
        assert not store.list(m.Observation, project_id=project.id)
    elif failure == "invalid_evaluation":
        assert store.list(m.Run, project_id=project.id)[0].evaluation.status == "invalid"
        assert not store.list(m.Observation, project_id=project.id)
    else:
        failed = store.list(m.Task, project_id=project.id, status="failed")
        assert len(failed) == (3 if failure == "ra" else 1)
        assert all(t.repair_attempts == 1 for t in failed)
        if failure == "ra":
            batch = next(
                json.loads(r.context_json)["exploration"]
                for r in backend.requests
                if r.role == "manager" and json.loads(r.context_json)["exploration"]
            )
            assert len(batch["outcomes"]) == 3
            assert all(o["output"] is None for o in batch["outcomes"])
        else:
            assert not store.reviews(project.id)


def test_max_cycles_and_stagnation_are_persistent_human_gates(setup):
    store, project, storage, backend, coding, runtime = setup

    async def no_progress(request):
        return json.dumps(
            {
                "summary": "Continue",
                "actions": [
                    {
                        "project_id": str(project.id),
                        "rationale": "No result yet",
                        "action": {
                            "action_type": "continue_research",
                            "next_question": "Same question",
                        },
                    }
                ],
            }
        )

    backend.complete = no_progress
    run(runtime)
    assert runtime.state.cycle == project.config.limits.stagnation_cycles
    assert "No new hypothesis" in runtime.state.gate.question
    runtime.answer(runtime.state.gate.id, "Try once more")
    runtime.state.cycle = project.config.limits.max_cycles
    runtime._save()
    run(runtime)
    assert "Maximum research" in runtime.state.gate.question


def test_experiment_budget_does_not_launch_extra_coding(setup):
    store, project, storage, backend, coding, runtime = setup
    # Reach proposal, then make budget exhausted before execution.
    run(runtime, 8)
    runtime.state.experiments_started = project.config.limits.max_experiments
    runtime._save()
    run(runtime)
    assert "Maximum experiment" in runtime.state.gate.question
    assert not coding.calls


def test_recovery_saved_manager_output_applies_once(setup):
    store, project, storage, backend, coding, runtime = setup
    original = runtime._accept_plan
    runtime._accept_plan = lambda outcome: (_ for _ in ()).throw(RuntimeError("crash after output"))
    with pytest.raises(RuntimeError):
        run(runtime)
    restarted = build_runtime(store, project, storage, backend, coding)
    restarted._accept_plan = original.__func__.__get__(restarted)
    run(restarted, 1)
    assert len(backend.requests) == 1
    assert len(store.list(m.Subproblem, project_id=project.id)) == 1
    assert restarted.state.operation is None


def test_recovery_partial_ra_batch_preserves_completed_outputs(setup):
    store, project, storage, backend, coding, runtime = setup
    run(runtime, 3)  # pending dispatch action
    action = runtime.state.actions[0]
    runtime.writer.apply(action, cycle=runtime.state.cycle)
    runtime._begin("research", action)
    asyncio.run(runtime.dispatcher.agent.explore(action.action.tasks[0]))
    restarted = build_runtime(store, project, storage, backend, coding)
    restarted.recover()
    assert len(restarted.state.batch.outcomes) == 3
    assert restarted.state.batch.outcomes[0].output is not None
    assert all(o.task.status == "failed" for o in restarted.state.batch.outcomes[1:])
    calls = len([r for r in backend.requests if r.role == "research_agent"])
    run(restarted)
    assert len([r for r in backend.requests if r.role == "research_agent"]) == calls
    assert restarted.project.status == "completed"


def test_recovery_execution_manifest_prevents_duplicate_coding(setup):
    store, project, storage, backend, coding, runtime = setup
    run(runtime, 9)  # execution action pending
    assert runtime.state.actions[0].action.action_type == "implement_experiment"
    original = runtime._execution_result
    runtime._execution_result = lambda result: (_ for _ in ()).throw(
        RuntimeError("crash after manifest")
    )
    with pytest.raises(RuntimeError):
        run(runtime, 1)
    assert len(coding.calls) == 1
    restarted = build_runtime(store, project, storage, backend, coding)
    restarted._execution_result = original.__func__.__get__(restarted)
    run(restarted)
    assert len(coding.calls) == 1
    assert len(store.list(m.Run, project_id=project.id)) == 1
    assert len(store.list(m.Observation, project_id=project.id)) == 1


def test_actual_process_exit_recovery_of_pending_manager(setup):
    store, project, storage, backend, coding, runtime = setup
    code = """import os,sys
from uuid import UUID
from pathlib import Path
from argos.state import StateStore
from argos.models import Project
from argos.core.host import build_runtime
from argos.backends import FakeLLMBackend
from argos.backends.coding import FakeCodingBackend
with StateStore(sys.argv[1]) as store:
 p=store.get(Project, UUID(sys.argv[2]))
 r=build_runtime(store,p,Path(sys.argv[1]).parent,FakeLLMBackend({}),FakeCodingBackend())
 r._begin('manager')
 os._exit(17)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(storage / "state.sqlite"), str(project.id)]
    )
    assert result.returncode == 17
    restarted = build_runtime(store, project, storage, backend, coding)
    run(restarted)
    assert store.list(m.Task, project_id=project.id, status="failed")[0].failure.startswith(
        "Runner interrupted"
    )
    assert restarted.project.status == "completed"


def test_runner_lock_is_exclusive_and_cli_status_pause_inspect(setup, capsys):
    store, project, storage, backend, coding, runtime = setup
    with runner_lock(storage):
        with pytest.raises(StateError):
            with runner_lock(storage):
                pass
        assert main(["--project", str(storage.parent), "status"]) == 0
        assert main(["--project", str(storage.parent), "pause"]) == 0
        assert main(["--project", str(storage.parent), "resume"]) == 1
    assert main(["--project", str(storage.parent), "resume"]) == 0
    assert main(["--project", str(storage.parent), "inspect", str(project.id)]) == 0
    assert str(project.id) in capsys.readouterr().out


def test_cli_init_and_explicit_refresh_preserve_baseline_history(tmp_path, capsys):
    directory = tmp_path / "project"
    demo.prepare(directory)
    args = ["--project", str(directory)]
    assert main(args + ["init"]) == 0
    with StateStore(directory / ".argos/state.sqlite") as store:
        project = store.list(m.Project)[0]
        first = project.baseline_id
    assert main(args + ["init"]) == 1
    assert main(args + ["baseline", "refresh", "--reason", "Explicit human refresh"]) == 0
    with StateStore(directory / ".argos/state.sqlite") as store:
        project = store.list(m.Project)[0]
        assert store.get(m.Baseline, project.baseline_id).previous_baseline_id == first
        assert len(store.list(m.Baseline, project_id=project.id)) == 2
    capsys.readouterr()
