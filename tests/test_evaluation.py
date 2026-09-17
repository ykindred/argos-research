"""Deterministic parser, subprocess, baseline and SQLite evaluation integration."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from argos import models as m
from argos.backends.coding import FakeCodingBackend
from argos.common import EvaluationProtocol, Measurement
from argos.evaluation import (
    BaselineRunner,
    CommandEvaluator,
    FakeEvaluator,
    approve_baseline,
    compare,
    parse_output,
    record_evaluation,
)
from argos.execution import ExperimentAgent
from argos.protocols import EvaluatorResult, ExperimentSpec, ProjectConfig
from argos.state import StateError, StateStore

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PAYLOAD = {
    "status": "ok",
    "metrics": {
        "cost": {"value": 8, "unit": "s", "direction": "minimize"},
        "quality": {"value": 0.9, "direction": "maximize"},
        "count": {"value": 0},
    },
    "constraints": {"correct": True},
    "artifacts": [],
}


def protocol():
    return EvaluationProtocol(
        name="synthetic-v1",
        command=[sys.executable, "evaluate.py"],
        metric_names=["cost", "quality", "count"],
        constraints=["correct"],
    )


def py(code):
    return [sys.executable, "-c", code]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.PIPE).decode()


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init")
    (repo / "src").mkdir()
    (repo / "src/value.json").write_text(json.dumps(PAYLOAD))
    (repo / "evaluate.py").write_text(
        "from pathlib import Path\nprint(Path('src/value.json').read_text())\n"
    )
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "fixture")
    project = ProjectConfig.model_validate_json(
        (EXAMPLES / "protocols/project_config.json").read_text()
    )
    project.source_repository = str(repo)
    project.evaluation_protocol = protocol()
    spec = ExperimentSpec.model_validate_json(
        (EXAMPLES / "protocols/experiment_spec.json").read_text()
    )
    spec.evaluation_protocol = protocol()
    spec.build_steps = [py("print('build')")]
    spec.test_steps = [py("print('test')")]
    spec.run_steps = [py("print('run')")]
    return repo, project, spec, tmp_path


def execute(setup, backend=None):
    _, project, spec, root = setup
    return asyncio.run(
        ExperimentAgent(project, backend or FakeCodingBackend(), root / "execution").execute(spec)
    )


def evaluate(setup, result, *, output=None, baseline=None):
    _, project, spec, root = setup
    evaluator = (
        CommandEvaluator(project, root / "evaluation")
        if output is None
        else FakeEvaluator(project, root / "evaluation", output)
    )
    return asyncio.run(evaluator.evaluate(spec, result, baseline=baseline))


def seed(store, project, spec, result):
    raw = json.loads((EXAMPLES / "entities.json").read_text())
    proj = m.Project.model_validate(raw["project"])
    proj.config = project
    store.create(proj)
    store.create(m.Subproblem.model_validate(raw["subproblem"]))
    store.create(m.ResearchBranch.model_validate(raw["research_branch"]))
    store.create(m.Hypothesis.model_validate(raw["hypothesis"]))
    store.create(
        m.Experiment(
            id=spec.experiment_id, created_at=result.started_at, spec=spec, status="running"
        )
    )
    store.create(
        m.Run(
            id=result.run_id,
            created_at=result.started_at,
            experiment_id=spec.experiment_id,
            status=result.status,
            result=result,
        )
    )
    return proj


def test_parser_multiple_metrics_and_constraints():
    output = parse_output(json.dumps(PAYLOAD), protocol())
    assert output.metrics["count"].direction == "informational"
    assert output.metrics["cost"].unit == "s"
    assert output.constraints == {"correct": True}
    data = json.loads(json.dumps(PAYLOAD))
    data["constraints"]["correct"] = {"passed": False, "measured_value": 0, "threshold": 1}
    assert parse_output(json.dumps(data), protocol()).constraints["correct"].passed is False


@pytest.mark.parametrize("value", [True, "12.3", None, float("nan"), float("inf"), -float("inf")])
def test_parser_rejects_non_numeric_or_non_finite(value):
    data = json.loads(json.dumps(PAYLOAD))
    data["metrics"]["cost"]["value"] = value
    with pytest.raises(ValueError):
        parse_output(json.dumps(data), protocol())


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "{}",
        '{"status":"ok","status":"ok"}',
        json.dumps({**PAYLOAD, "metrics": {}}),
        json.dumps({**PAYLOAD, "constraints": {}}),
        json.dumps({**PAYLOAD, "constraints": {"correct": "true"}}),
        json.dumps({**PAYLOAD, "constraints": {"correct": 1}}),
        json.dumps({**PAYLOAD, "status": "invalid"}),
        json.dumps({**PAYLOAD, "conclusion": "hypothesis supported"}),
        json.dumps(PAYLOAD) + " trailing log",
    ],
)
def test_parser_rejects_invalid_shape_and_missing_required_fields(raw):
    with pytest.raises(ValueError):
        parse_output(raw, protocol())


def test_baseline_arithmetic_handles_zero_negative_and_metadata():
    def metric(value, **kwargs):
        return Measurement(name="x", value=value, direction="informational", **kwargs)

    assert compare([metric(0)], [metric(0)])[0].percent_change is None
    assert compare([metric(-8)], [metric(-10)])[0].percent_change == 20
    assert compare([metric(10)], [metric(10)])[0].delta == 0
    with pytest.raises(ValueError, match="Incompatible"):
        compare([metric(1, unit="ms")], [metric(1, unit="s")])
    with pytest.raises(ValueError):
        compare([metric(1e308)], [metric(-1e308)])


def test_execution_evaluation_observation_restart_and_provenance(setup):
    repo, project, spec, root = setup
    data = json.loads(json.dumps(PAYLOAD))
    data["constraints"]["correct"] = False
    data["artifacts"] = ["raw.txt"]
    spec.run_steps.append(
        py("from pathlib import Path; Path('raw.txt').write_text('raw evidence')")
    )
    result = execute(setup, FakeCodingBackend({"src/value.json": json.dumps(data)}))
    evaluation = evaluate(setup, result)
    assert evaluation.status == "ok"
    assert not evaluation.constraint_checks[0].passed
    assert len(evaluation.measurements) == 3
    assert Path(evaluation.artifacts[0]).read_text() == "raw evidence"
    provenance = evaluation.provenance
    assert provenance.command.argv == spec.evaluation_protocol.command
    assert provenance.command.exit_code == 0
    assert json.loads(Path(provenance.input_path).read_text())["execution"]["run_id"] == str(
        result.run_id
    )
    assert Path(provenance.stdout_path).read_text() == provenance.command.stdout
    manifest = Path(provenance.input_path).parent / "evaluation.json"
    assert EvaluatorResult.model_validate_json(manifest.read_text()) == evaluation
    with StateStore(root / "state.sqlite") as store:
        seed(store, project, spec, result)
        original = store.get(m.Hypothesis, spec.hypothesis_id)
        observation = record_evaluation(store, evaluation)
        assert observation.relation == "neutral"
        assert "constraint correct: failed" in observation.summary
        assert store.get(m.Experiment, spec.experiment_id).status == "completed"
        assert store.get(m.Hypothesis, spec.hypothesis_id) == original
        assert record_evaluation(store, evaluation) == observation
        assert len(store.list(m.Observation)) == 1
    with StateStore(root / "state.sqlite") as store:
        loaded = store.get(m.Observation, observation.id)
        run = store.get(m.Run, loaded.run_id)
        experiment = store.get(m.Experiment, run.experiment_id)
        assert experiment.spec.hypothesis_id == spec.hypothesis_id
        assert run.evaluation == loaded.evaluation == evaluation
        assert run.result == result
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("raw", ["{bad", json.dumps({**PAYLOAD, "metrics": {}})])
def test_invalid_output_persists_failure_without_observation(setup, raw):
    _, project, spec, root = setup
    result = execute(setup)
    evaluation = evaluate(setup, result, output=raw)
    assert evaluation.status == "invalid"
    assert evaluation.measurements == []
    assert evaluation.failure.kind == "invalid_evaluator_output"
    with StateStore(root / "state.sqlite") as store:
        seed(store, project, spec, result)
        hypothesis = store.get(m.Hypothesis, spec.hypothesis_id)
        assert record_evaluation(store, evaluation) is None
        assert store.get(m.Experiment, spec.experiment_id).status == "invalid_result"
        assert not store.list(m.Observation)
        assert store.get(m.Hypothesis, spec.hypothesis_id) == hypothesis
    with StateStore(root / "state.sqlite") as store:
        assert store.get(m.Run, result.run_id).evaluation == evaluation


@pytest.mark.parametrize(
    "command,kind",
    [
        (py("import sys; print('crash'); sys.exit(3)"), "invalid_evaluator_output"),
        (["/nonexistent/argos-evaluator"], "invalid_evaluator_output"),
        (py("import time; print('begin', flush=True); time.sleep(30)"), "timeout"),
    ],
)
def test_command_failure_is_explicit_and_preserves_logs(setup, command, kind):
    _, project, spec, _ = setup
    project.evaluation_protocol.command = command
    spec.evaluation_protocol.command = command
    spec.resource_limits.timeout_seconds = 1
    result = execute(setup)
    evaluation = evaluate(setup, result)
    assert evaluation.status == "invalid"
    assert evaluation.failure.kind == kind
    assert evaluation.provenance.command is not None
    assert Path(evaluation.provenance.stderr_path).exists()
    assert Path(evaluation.provenance.stdout_path).exists()


def test_evaluator_tampering_and_failed_execution_rejected(setup):
    _, project, spec, _ = setup
    result = execute(setup)
    spec.evaluation_protocol = spec.evaluation_protocol.model_copy(update={"command": ["other"]})
    with pytest.raises(ValueError, match="protected"):
        evaluate(setup, result)
    spec.evaluation_protocol = project.evaluation_protocol
    spec.run_steps = [py("raise RuntimeError('failed')")]
    failed = execute(setup)
    with pytest.raises(ValueError, match="successful"):
        evaluate(setup, failed)


@pytest.mark.parametrize("artifact", ["../escape", "/etc/passwd", "missing.txt", "linked.txt"])
def test_artifact_references_cannot_escape_or_be_missing(setup, artifact):
    _, _, _, root = setup
    result = execute(setup)
    (root / "outside.txt").write_text("outside")
    (Path(result.worktree) / "linked.txt").symlink_to(root / "outside.txt")
    output = {**PAYLOAD, "artifacts": [artifact]}
    evaluation = evaluate(setup, result, output=json.dumps(output))
    assert evaluation.status == "invalid"
    assert evaluation.measurements == []


def test_source_drift_before_and_during_evaluation_is_invalid(setup):
    _, project, spec, _ = setup
    result = execute(setup)
    (Path(result.worktree) / "evaluate.py").write_text("raise RuntimeError('tampered')")
    invalid = evaluate(setup, result)
    assert invalid.status == "invalid"
    assert invalid.provenance.command is None
    command = py("from pathlib import Path; Path('evaluate.py').write_text('changed')")
    project.evaluation_protocol.command = command
    spec.evaluation_protocol.command = command
    result = execute(setup)
    invalid = evaluate(setup, result)
    assert invalid.status == "invalid"
    assert invalid.provenance.command.exit_code == 0


def test_clean_baseline_refresh_is_explicit_and_old_experiment_stays_pinned(setup):
    repo, project, spec, root = setup
    # Dirty canonical files are excluded by the clean detached baseline checkout.
    (repo / "src/value.json").write_text("not baseline data")
    runner = BaselineRunner(project, root / "baselines")
    result = asyncio.run(runner.execute(spec))
    assert result.status == "succeeded"
    evaluation = evaluate(setup, result)
    with StateStore(root / "state.sqlite") as store:
        proj = seed(store, project, spec, result)
        record_evaluation(store, evaluation)
        assert store.get(m.Project, proj.id).baseline_id is None
        baseline = approve_baseline(store, proj.id, result.run_id, rationale="Human init")
        assert baseline.source_commit == result.source_commit == result.resulting_commit
        old_spec = spec.model_copy(deep=True)
        old_spec.experiment_id = uuid4()
        old_spec.baseline_id = baseline.id
        old_setup = (repo, project, old_spec, root)
        current = execute(old_setup)
        measured = evaluate(old_setup, current, baseline=baseline)
        assert measured.status == "ok"
        assert [c.delta for c in measured.comparisons] == [0, 0, 0]
        assert measured.comparisons[-1].percent_change is None
        store.create(
            m.Experiment(
                id=old_spec.experiment_id,
                created_at=current.started_at,
                spec=old_spec,
                status="running",
            )
        )
        store.create(
            m.Run(
                id=current.run_id,
                created_at=current.started_at,
                experiment_id=old_spec.experiment_id,
                status="succeeded",
                result=current,
            )
        )
        obs = record_evaluation(store, measured)
        refresh = approve_baseline(store, proj.id, result.run_id, rationale="Human refresh")
        assert refresh.previous_baseline_id == baseline.id
        assert store.get(m.Project, proj.id).baseline_id == refresh.id
        assert store.get(m.Observation, obs.id).evaluation.baseline_id == baseline.id
        assert len(store.list(m.Baseline)) == 2
        with pytest.raises(StateError, match="append-only"):
            store.update(baseline.model_copy(update={"source_commit": "changed"}))
        with pytest.raises(StateError):
            approve_baseline(store, proj.id, current.run_id, rationale="Not a baseline run")
        with pytest.raises(ValueError, match="pinned baseline"):
            evaluate(old_setup, current, baseline=refresh)
    assert (repo / "src/value.json").read_text() == "not baseline data"


@pytest.mark.parametrize(
    "steps,kind",
    [
        ([py("raise RuntimeError('test failed')")], "test_failure"),
        (
            [py("from pathlib import Path; Path('evaluate.py').write_text('changed')")],
            "runtime_crash",
        ),
        ([py("import time; time.sleep(30)")], "timeout"),
    ],
)
def test_baseline_failure_keeps_evidence(setup, steps, kind):
    _, project, spec, root = setup
    spec.test_steps = steps
    spec.resource_limits.timeout_seconds = 1
    result = asyncio.run(BaselineRunner(project, root / "baseline").execute(spec))
    assert result.status == "failed"
    assert result.failure.kind == kind
    assert Path(result.diff_path).exists()
    assert Path(result.stdout_path).exists()
    assert (
        json.loads((Path(result.diff_path).parent / "result.json").read_text())["status"]
        == "failed"
    )


def test_state_attachment_rolls_back_and_cannot_replace_measurements(setup):
    _, project, spec, root = setup
    result = execute(setup)
    evaluation = evaluate(setup, result)
    with StateStore(root / "state.sqlite") as store:
        seed(store, project, spec, result)
        bad = evaluation.model_copy(update={"protocol_name": "other"})
        with pytest.raises(StateError):
            record_evaluation(store, bad)
        assert store.get(m.Run, result.run_id).evaluation is None
        assert not store.list(m.Observation)
        record_evaluation(store, evaluation)
        changed = evaluation.model_copy(deep=True)
        changed.measurements[0].value = 123
        with pytest.raises(StateError, match="different evaluation"):
            record_evaluation(store, changed)


def test_fake_evaluator_matches_command_measurements(setup):
    result = execute(setup)
    real = evaluate(setup, result)
    fake = evaluate(setup, result, output=json.dumps(PAYLOAD))
    assert real.measurements == fake.measurements
    assert real.constraint_checks == fake.constraint_checks
    assert real.status == fake.status == "ok"
    assert fake.provenance.command.argv == ["FakeEvaluator"]


def test_comparison_requires_baseline():
    data = json.loads((EXAMPLES / "protocols/evaluator_result.json").read_text())
    data["comparisons"] = [
        {"name": "x", "baseline_value": 1, "value": 2, "delta": 1, "direction": "minimize"}
    ]
    with pytest.raises(ValidationError):
        EvaluatorResult.model_validate(data)


def test_evaluator_cancellation_retains_manifest_and_command(setup):
    _, project, spec, root = setup
    command = py("import time; print('ready', flush=True); time.sleep(30)")
    project.evaluation_protocol.command = command
    spec.evaluation_protocol.command = command
    result = execute(setup)
    evaluator = CommandEvaluator(project, root / "evaluation")

    async def cancel():
        task = asyncio.create_task(evaluator.evaluate(spec, result))
        for _ in range(200):
            logs = list((root / "evaluation").glob("*/*/stdout.log"))
            if logs and "ready" in logs[0].read_text():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Evaluator did not start")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    manifest = next((root / "evaluation").glob("*/*/evaluation.json"))
    evaluation = EvaluatorResult.model_validate_json(manifest.read_text())
    assert evaluation.status == "invalid"
    assert "cancelled" in evaluation.failure.message
    assert evaluation.provenance.command.exit_code is not None
    assert "ready" in evaluation.provenance.command.stdout


def test_evaluation_queue_wait_counts_toward_timeout(setup):
    _, project, spec, root = setup
    spec.resource_limits.timeout_seconds = 1
    result = execute(setup)

    async def queued():
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        evaluator = CommandEvaluator(project, root / "evaluation", cpu_sem=semaphore)
        return await evaluator.evaluate(spec, result)

    evaluation = asyncio.run(queued())
    assert evaluation.failure.kind == "timeout"
    assert evaluation.provenance.command is None
    with StateStore(root / "state.sqlite") as store:
        seed(store, project, spec, result)
        assert record_evaluation(store, evaluation) is None
        assert store.get(m.Experiment, spec.experiment_id).status == "timeout"


def test_pinned_baseline_comparison_records_actual_change(setup):
    repo, project, spec, root = setup
    result = asyncio.run(BaselineRunner(project, root / "baselines").execute(spec))
    evaluation = evaluate(setup, result)
    with StateStore(root / "state.sqlite") as store:
        proj = seed(store, project, spec, result)
        record_evaluation(store, evaluation)
        baseline = approve_baseline(store, proj.id, result.run_id, rationale="Human init")
        spec = spec.model_copy(deep=True)
        spec.experiment_id = uuid4()
        spec.baseline_id = baseline.id
        changed = json.loads(json.dumps(PAYLOAD))
        changed["metrics"]["cost"]["value"] = 6
        changed["constraints"]["correct"] = False
        current_setup = repo, project, spec, root
        current = execute(current_setup, FakeCodingBackend({"src/value.json": json.dumps(changed)}))
        measured = evaluate(current_setup, current, baseline=baseline)
        assert measured.status == "ok"
        assert measured.comparisons[0].delta == -2
        assert measured.comparisons[0].percent_change == -25
        assert not measured.constraint_checks[0].passed
        # No silent overwrite, even when the measurements improve.
        assert store.get(m.Project, proj.id).baseline_id == baseline.id
        assert store.get(m.Baseline, baseline.id).evaluation.measurements[0].value == 8
        changed["metrics"]["cost"]["unit"] = "ms"
        invalid = evaluate(current_setup, current, baseline=baseline, output=json.dumps(changed))
        assert invalid.status == "invalid"
        assert not invalid.comparisons


def test_baseline_cannot_be_approved_when_constraints_fail(setup):
    _, project, spec, root = setup
    result = asyncio.run(BaselineRunner(project, root / "baselines").execute(spec))
    evaluation = evaluate(
        setup, result, output=json.dumps({**PAYLOAD, "constraints": {"correct": False}})
    )
    with StateStore(root / "state.sqlite") as store:
        proj = seed(store, project, spec, result)
        record_evaluation(store, evaluation)
        with pytest.raises(StateError, match="constraint-passing"):
            approve_baseline(store, proj.id, result.run_id, rationale="Human init")
        assert not store.list(m.Baseline)
        assert not store.list(m.Decision)


def test_baseline_preserves_required_artifacts_and_detects_missing(setup):
    _, project, spec, root = setup
    spec.required_artifacts = ["generated.txt"]
    runner = BaselineRunner(project, root / "baselines")
    missing = asyncio.run(runner.execute(spec))
    assert missing.failure.kind == "missing_artifact"
    spec.run_steps.append(
        py("from pathlib import Path; Path('generated.txt').write_text('evidence')")
    )
    success = asyncio.run(runner.execute(spec))
    assert success.status == "succeeded"
    assert Path(success.artifacts[0]).read_text() == "evidence"
    assert "generated.txt" in Path(success.diff_path).read_text()


def test_baseline_git_damage_returns_failure_and_preserves_logs(setup):
    _, project, spec, root = setup
    spec.run_steps = [py("from pathlib import Path; Path('.git').unlink(); print('git damaged')")]
    result = asyncio.run(BaselineRunner(project, root / "baselines").execute(spec))
    assert result.status == "failed"
    assert "git damaged" in Path(result.stdout_path).read_text()
    assert (Path(result.diff_path).parent / "preservation-error.log").is_file()
    assert (Path(result.diff_path).parent / "result.json").is_file()
