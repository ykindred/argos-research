"""Real SQLite persistence, atomicity, provenance and human-boundary regressions."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from argos import models as m
from argos.common import EntityReference
from argos.protocols import CriticReview
from argos.state import StateError, StateStore
from argos.state.store import TYPES

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def example(name, cls):
    return cls.model_validate(json.loads((EXAMPLES / "entities.json").read_text())[name])


def changed(entity, **updates):
    return type(entity).model_validate(dict(entity.model_dump(), **updates))


@pytest.fixture
def store(tmp_path):
    with StateStore(tmp_path / "research.sqlite") as store:
        yield store


def seed(store):
    entities = {}
    with store.transaction():
        for name in (
            "project",
            "subproblem",
            "research_branch",
            "hypothesis",
            "experiment",
            "run",
            "observation",
            "claim",
            "decision",
            "task",
        ):
            entities[name] = store.create(example(name, TYPES[name]))
    return entities


def test_all_entities_and_complete_provenance_survive_reopen(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        entities = seed(store)
        store.create(example("baseline_experiment", m.Experiment))
        store.create(example("baseline_run", m.Run))
        baseline = store.approve_baseline(example("baseline", m.Baseline), rationale="Human init")
        evidence = store.list(m.Evidence)[0]
        assert evidence.relation == "contradicts"
        assert set(TYPES.values()) == {type(e) for e in entities.values()} | {
            m.Evidence,
            m.Baseline,
        }
    with StateStore(path) as store:
        for key, value in entities.items():
            if key != "project":
                assert store.get(type(value), value.id) == value
        assert store.get(m.Project, entities["project"].id).baseline_id == baseline.id
        assert store.get(m.Baseline, baseline.id) == baseline
        assert store.get(m.Evidence, evidence.id) == evidence
        trace = store.claim_provenance(entities["claim"].id)[0]
        for name in ("claim", "observation", "run", "experiment", "hypothesis"):
            assert getattr(trace, name) == entities[name]
        assert trace.run.result.configuration == {"seed": 42, "cache": True}
        assert trace.run.result.diff_path
        assert trace.run.result.stdout_path
        assert trace.run.result.stderr_path
        assert store.list(m.Experiment, parent_id=entities["hypothesis"].id, role="hypothesis_id")
        assert store.list(m.Run, parent_id=entities["experiment"].id) == [entities["run"]]
        assert store.list(m.Hypothesis, status="contradicted") == [entities["hypothesis"]]


def test_actual_process_restart_and_uncommitted_crash(tmp_path):
    path = tmp_path / "state.sqlite"
    fixture = EXAMPLES / "entities.json"
    script = """
import json, os, sys
from argos.models import Project
from argos.state import StateStore
p = Project.model_validate(json.load(open(sys.argv[2]))["project"])
s = StateStore(sys.argv[1])
s.create(p)
with s.transaction():
    p.name = "uncommitted"
    s.update(p)
    os._exit(19)
"""
    result = subprocess.run([sys.executable, "-c", script, str(path), str(fixture)], check=False)
    assert result.returncode == 19
    project = example("project", m.Project)
    with StateStore(path) as store:
        assert store.get(m.Project, project.id) == project
        assert store.history(m.Project, project.id) == [project]
    reader = """
import sys
from argos.models import Project
from argos.state import StateStore
with StateStore(sys.argv[1]) as s:
    print(s.get(Project, sys.argv[2]).model_dump_json())
"""
    result = subprocess.run(
        [sys.executable, "-c", reader, str(path), str(project.id)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert m.Project.model_validate_json(result.stdout) == project


def test_missing_wrong_type_and_cross_project_references_roll_back(store):
    project = store.create(example("project", m.Project))
    subproblem = example("subproblem", m.Subproblem)
    with pytest.raises(StateError):
        store.create(changed(subproblem, project_id=uuid4()))
    store.create(subproblem)
    with pytest.raises(StateError):
        store.create(changed(example("hypothesis", m.Hypothesis), subproblem_id=project.id))
    branch = store.create(example("research_branch", m.ResearchBranch))
    other_project = store.create(changed(project, id=uuid4()))
    other_subproblem = store.create(changed(subproblem, id=uuid4(), project_id=other_project.id))
    with pytest.raises(StateError, match="another subproblem"):
        store.create(
            changed(
                example("hypothesis", m.Hypothesis),
                subproblem_id=other_subproblem.id,
                branch_id=branch.id,
            )
        )
    with pytest.raises(StateError, match="Cross-project"):
        store.create(changed(subproblem, id=uuid4(), parent_id=other_subproblem.id))
    assert store.list(m.Hypothesis) == []


def test_atomic_batch_and_nested_savepoint(store):
    project = example("project", m.Project)
    with pytest.raises(StateError):
        with store.transaction():
            store.create(project)
            store.create(changed(example("subproblem", m.Subproblem), project_id=uuid4()))
    assert store.list(m.Project) == []
    with store.transaction():
        store.create(project)
        with pytest.raises(StateError):
            store.create(project)
        store.create(example("subproblem", m.Subproblem))
    assert len(store.list(m.Subproblem)) == 1
    assert len(store.history(m.Project, project.id)) == 1


def test_boundary_revalidates_inplace_and_model_copy_mutations(store):
    project = example("project", m.Project)
    project.config.build_command.clear()
    with pytest.raises(ValidationError):
        store.create(project)
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        with pytest.raises(ValidationError):
            store.create(example("project", m.Project).model_copy(update={"status": "invented"}))
    assert not store.list(m.Project)


def test_main_question_requires_exact_explicit_human_approval(store):
    project = store.create(example("project", m.Project))
    assert store.snapshot(project.id).main_research_question is None
    for updates in ({"main_question_approved": True}, {"baseline_id": uuid4()}):
        with pytest.raises(StateError):
            store.update(changed(project, **updates))
    data = project.model_dump()
    data["config"]["main_research_question"] = "Unapproved question"
    with pytest.raises(StateError):
        store.update(m.Project.model_validate(data))
    project = store.approve_main_question(
        project.id,
        project.config.main_research_question,
        rationale="Human initialized this research",
    )
    original = project.config.main_research_question
    project.proposed_main_research_question = "Can allocation cost be reduced?"
    store.update(project)
    assert store.snapshot(project.id).main_research_question == original
    with pytest.raises(StateError):
        store.approve_main_question(project.id, "Different proposal", rationale="Approve")
    with pytest.raises(ValidationError):
        store.approve_main_question(
            project.id, project.proposed_main_research_question, rationale=" "
        )
    assert len(store.list(m.Decision)) == 1
    project = store.approve_main_question(
        project.id,
        project.proposed_main_research_question,
        rationale="Human explicitly approved revised wording",
    )
    assert project.main_question_approved
    assert project.proposed_main_research_question is None
    decisions = store.list(m.Decision, project_id=project.id)
    assert len(decisions) == 2 and all(d.actor == "human" for d in decisions)
    assert store.history(m.Project, project.id)[0].config.main_research_question == original
    # Forging a human label on a generic Decision does not change canonical settings.
    forged = changed(decisions[0], id=uuid4(), summary="Forged approval")
    store.create(forged)
    with pytest.raises(StateError):
        store.update(m.Project.model_validate(data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("research_direction", "Another direction"),
        ("research_charter", "Another charter"),
        ("held_out_test_protocol", "Another test"),
        (
            "evaluation_protocol",
            {"name": "new", "command": ["new"], "metric_names": ["x"], "constraints": []},
        ),
    ],
)
def test_protected_config_cannot_be_replaced(store, field, value):
    project = store.create(example("project", m.Project))
    data = project.model_dump()
    data["config"][field] = value
    with pytest.raises(StateError):
        store.update(m.Project.model_validate(data))
    assert store.get(m.Project, project.id) == project


def test_reference_identity_and_parent_cycles_cannot_be_rewritten(store):
    entities = seed(store)
    parent = entities["subproblem"]
    child = store.create(changed(parent, id=uuid4(), parent_id=parent.id))
    with pytest.raises(StateError):
        store.update(changed(parent, parent_id=child.id))
    with pytest.raises(StateError):
        store.update(changed(entities["hypothesis"], subproblem_id=child.id))
    with pytest.raises(StateError):
        store.create(
            changed(
                entities["decision"],
                id=uuid4(),
                references=[EntityReference(entity_type="run", entity_id=uuid4())],
            )
        )
    with pytest.raises(StateError):
        store.create(
            changed(
                entities["task"],
                id=uuid4(),
                output_references=[EntityReference(entity_type="hypothesis", entity_id=uuid4())],
            )
        )


def test_evaluation_cannot_be_fabricated_or_replaced(store):
    entities = seed(store)
    observation = entities["observation"]
    data = observation.model_dump()
    data["id"] = uuid4()
    data["evaluation"]["measurements"][0]["value"] = 0.1
    with pytest.raises(StateError, match="authoritative"):
        store.create(m.Observation.model_validate(data))
    data = entities["run"].model_dump()
    data["evaluation"]["measurements"][0]["value"] = 0.1
    with pytest.raises(StateError, match="immutable"):
        store.update(m.Run.model_validate(data))
    assert store.get(m.Observation, observation.id) == observation


def test_success_then_evaluation_attach_once(store):
    entities = seed(store)
    run = entities["run"]
    data = run.model_dump()
    ident = uuid4()
    data["id"] = ident
    data["result"]["run_id"] = ident
    data["evaluation"]["run_id"] = ident
    evaluated = m.Run.model_validate(data)
    unevaluated = changed(evaluated, evaluation=None)
    store.create(unevaluated)
    assert store.update(evaluated) == evaluated
    with pytest.raises(StateError):
        store.update(unevaluated)


def test_failures_stay_distinct_from_scientific_contradiction(store):
    entities = seed(store)
    raw = json.loads((EXAMPLES / "protocols/experiment_result_failure.json").read_text())
    from argos.protocols import ExperimentResult

    result = ExperimentResult.model_validate(raw)
    run = m.Run(
        id=result.run_id,
        created_at=result.started_at,
        experiment_id=result.experiment_id,
        status="failed",
        result=result,
    )
    store.create(run)
    snapshot = store.snapshot(entities["project"].id)
    assert snapshot.failed_runs == [run]
    assert snapshot.failed_directions == [entities["hypothesis"]]
    assert entities["run"].status == "succeeded"
    assert snapshot.failed_runs[0].result.failure is not None
    assert len(store.list(m.Observation)) == 1
    assert store.get(m.Hypothesis, entities["hypothesis"].id) == entities["hypothesis"]
    with pytest.raises(StateError):
        store.update(changed(run, status="pending", result=None))


def test_invalid_evaluation_cannot_be_claim_evidence(store):
    entities = seed(store)
    data = entities["run"].model_dump()
    ident = uuid4()
    data["id"] = ident
    data["result"]["run_id"] = ident
    data["evaluation"].update(run_id=ident, status="invalid", measurements=[])
    run = store.create(m.Run.model_validate(data))
    observation = store.create(
        changed(
            entities["observation"],
            id=uuid4(),
            run_id=run.id,
            evaluation=run.evaluation,
            relation="invalid",
        )
    )
    with pytest.raises(StateError, match="Invalid observation"):
        store.create(
            changed(
                entities["claim"],
                id=uuid4(),
                evidence={
                    "supporting_observation_ids": [observation.id],
                    "contradicting_observation_ids": [],
                },
            )
        )


def test_evidence_projection_is_atomic_and_append_only(store):
    entities = seed(store)
    claim = entities["claim"]
    evidence = store.list(m.Evidence)[0]
    assert store.get(m.Evidence, evidence.id).run_id == entities["run"].id
    with pytest.raises(StateError):
        store.create(changed(evidence, id=uuid4(), run_id=uuid4()))
    with pytest.raises(StateError, match="append-only"):
        store.update(
            changed(
                claim,
                evidence={
                    "supporting_observation_ids": [entities["observation"].id],
                    "contradicting_observation_ids": [],
                },
            )
        )
    observation = store.create(
        changed(entities["observation"], id=uuid4(), summary="Second analysis")
    )
    claim.evidence.supporting_observation_ids.append(observation.id)
    store.update(claim)
    assert len(store.claim_provenance(claim.id)) == 2
    assert len(store.history(m.Claim, claim.id)) == 2
    # Conflicting generated ID rolls the whole claim write back.
    from uuid import uuid5

    new_id = uuid4()
    collision = uuid5(new_id, str(observation.id))
    store.create(changed(entities["subproblem"], id=collision))
    with pytest.raises(StateError):
        store.create(changed(claim, id=new_id))
    with pytest.raises(KeyError):
        store.get(m.Claim, new_id)


def test_reviews_preserve_rejected_claim_version(store):
    entities = seed(store)
    for verdict in ("accept", "reject", "needs_more_evidence"):
        review = CriticReview(
            claim_id=entities["claim"].id,
            created_at=entities["claim"].created_at,
            verdict=verdict,
            rationale="Review rationale",
            weaknesses=[],
            risks=[],
            requested_checks=["Replicate measurement"],
        )
        store.record_review(review, reviewed_claim=entities["claim"])
    store.update(changed(entities["claim"], statement="Revised wording", status="active"))
    reviews = store.reviews(entities["project"].id)
    assert {r.review.verdict for r in reviews} == {"accept", "reject", "needs_more_evidence"}
    assert all(r.claim == entities["claim"] for r in reviews)
    assert len(store.snapshot(entities["project"].id).reviews) == 3
    with pytest.raises(StateError, match="Claim changed"):
        store.record_review(review, reviewed_claim=entities["claim"])
    assert len(store.reviews(entities["project"].id)) == 3


def test_baseline_refresh_is_explicit_atomic_and_keeps_history(store):
    entities = seed(store)
    store.create(example("baseline_experiment", m.Experiment))
    store.create(example("baseline_run", m.Run))
    baseline = example("baseline", m.Baseline)
    with pytest.raises(StateError):
        store.create(baseline)
    store.approve_baseline(baseline, rationale="Human init")
    fresh = changed(
        baseline, id=uuid4(), previous_baseline_id=baseline.id, approval_decision_id=uuid4()
    )
    store.approve_baseline(fresh, rationale="Human refresh")
    assert store.get(m.Project, entities["project"].id).baseline_id == fresh.id
    assert store.get(m.Baseline, baseline.id) == baseline
    with pytest.raises(StateError):
        store.update(changed(baseline, source_commit="changed"))
    with pytest.raises(StateError):
        store.approve_baseline(
            changed(fresh, id=uuid4(), approval_decision_id=uuid4()), rationale="Stale refresh"
        )
    invalid = changed(
        fresh,
        id=uuid4(),
        previous_baseline_id=fresh.id,
        approval_decision_id=uuid4(),
        source_commit="not the run commit",
    )
    before = len(store.list(m.Decision))
    with pytest.raises(StateError):
        store.approve_baseline(invalid, rationale="Should roll back")
    assert len(store.list(m.Decision)) == before
    assert len(store.list(m.Baseline)) == 2


def test_bounded_snapshot_is_project_scoped_with_preserved_history(store):
    entities = seed(store)
    project = entities["project"]
    second = store.create(changed(project, id=uuid4(), name="Other research"))
    for i in range(3):
        store.create(changed(entities["subproblem"], id=uuid4(), question=f"Local {i}"))
        store.create(changed(entities["subproblem"], id=uuid4(), project_id=second.id))
    store.update(changed(entities["research_branch"], status="archived"))
    snapshot = store.snapshot(project.id, limit=2)
    assert len(snapshot.active_subproblems) == 2
    assert snapshot.truncated == ["active_subproblems"]
    assert all(s.project_id == project.id for s in snapshot.active_subproblems)
    assert snapshot.abandoned_branches[0].id == entities["research_branch"].id
    assert snapshot.failed_directions[0].status == "contradicted"
    hypothesis = changed(entities["hypothesis"], status="testing")
    store.update(hypothesis)
    assert store.history(m.Hypothesis, hypothesis.id)[0].status == "contradicted"
    assert len(store.list(m.Subproblem, project_id=project.id, limit=2, offset=2)) == 2
    for invalid in (0, -1, 1001, True):
        with pytest.raises(ValueError):
            store.snapshot(project.id, limit=invalid)


def test_experiment_transitions_and_terminal_task(store):
    entities = seed(store)
    with pytest.raises(StateError):
        store.update(changed(entities["experiment"], status="running"))
    with pytest.raises(StateError):
        store.update(changed(entities["task"], status="pending", finished_at=None))
    data = entities["experiment"].model_dump()
    ident = uuid4()
    data.update(id=ident, status="planned")
    data["spec"]["experiment_id"] = ident
    experiment = store.create(m.Experiment.model_validate(data))
    for status in ("implementing", "implemented", "testing", "running", "evaluating", "completed"):
        experiment = store.update(changed(experiment, status=status))
    assert len(store.history(m.Experiment, ident)) == 7


def test_sqlite_foreign_keys_and_schema_version(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        assert store._db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute("INSERT INTO links VALUES ('missing','run_id','missing')")
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(StateError, match="Unsupported"):
        StateStore(path)


def test_two_connections_see_committed_updates(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as first, StateStore(path) as second:
        project = first.create(example("project", m.Project))
        assert second.get(m.Project, project.id) == project
        project.name = "Updated"
        second.update(project)
        assert first.get(m.Project, project.id).name == "Updated"


def test_tested_hypothesis_and_evaluator_cannot_drift(store):
    entities = seed(store)
    with pytest.raises(StateError, match="tested hypothesis"):
        store.update(changed(entities["hypothesis"], statement="Different hypothesis"))
    data = entities["experiment"].model_dump()
    ident = uuid4()
    data["id"] = ident
    data["spec"]["experiment_id"] = ident
    data["spec"]["hypothesis_statement"] = "Different hypothesis"
    with pytest.raises(StateError, match="hypothesis statement"):
        store.create(m.Experiment.model_validate(data))
    data["spec"]["hypothesis_statement"] = entities["hypothesis"].statement
    data["spec"]["evaluation_protocol"]["command"] = ["replacement-evaluator"]
    with pytest.raises(StateError, match="protected evaluator"):
        store.create(m.Experiment.model_validate(data))


def test_cross_project_claim_and_task_rejected(store):
    entities = seed(store)
    other = store.create(changed(entities["project"], id=uuid4()))
    with pytest.raises(StateError, match="Cross-project"):
        store.create(changed(entities["claim"], id=uuid4(), project_id=other.id))
    with pytest.raises(StateError, match="Cross-project"):
        store.create(changed(entities["task"], id=uuid4(), project_id=other.id))
    with pytest.raises(StateError, match="Cross-project"):
        store.create(changed(entities["decision"], id=uuid4(), project_id=other.id, action=None))


def test_failed_task_and_review_persist_after_reopen(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        entities = seed(store)
        failed = store.create(
            changed(
                entities["task"],
                id=uuid4(),
                status="failed",
                failure="Invalid output after one repair",
                repair_attempts=1,
            )
        )
        review = CriticReview(
            claim_id=entities["claim"].id,
            created_at=entities["claim"].created_at,
            verdict="reject",
            rationale="Insufficient evidence",
            weaknesses=["No replication"],
            risks=[],
            requested_checks=["Replicate"],
        )
        review_id = store.record_review(review, reviewed_claim=entities["claim"])
    with StateStore(path) as store:
        assert store.get(m.Task, failed.id) == failed
        stored = store.reviews(entities["project"].id)[0]
        assert stored.id == review_id
        assert stored.review == review
        assert stored.claim == entities["claim"]


def test_read_snapshot_uses_one_database_transaction(store):
    entities = seed(store)
    statements = []
    store._db.set_trace_callback(statements.append)
    snapshot = store.snapshot(entities["project"].id)
    store._db.set_trace_callback(None)
    assert snapshot.project.id == entities["project"].id
    assert statements[0] == "BEGIN"
    assert statements[-1] == "ROLLBACK"
    assert statements.count("BEGIN") == 1


def test_task_repair_budget_and_dispatch_survive_restart(tmp_path):
    path = tmp_path / "state.sqlite"
    with StateStore(path) as store:
        entities = seed(store)
        task = store.create(
            changed(entities["task"], id=uuid4(), status="running", finished_at=None)
        )
        repaired = store.update(changed(task, repair_attempts=1))
    with StateStore(path) as store:
        for updates in (
            {"repair_attempts": 0},
            {"kind": "implement"},
            {"resource_class": "coding"},
            {"references": []},
            {"started_at": "2026-09-18T08:00:01Z"},
        ):
            with pytest.raises(StateError):
                store.update(changed(repaired, **updates))
            assert store.get(m.Task, task.id) == repaired
            assert store.history(m.Task, task.id) == [task, repaired]
        failed = store.update(
            changed(
                repaired,
                status="failed",
                failure="Structured output invalid after one repair",
                finished_at="2026-09-18T08:01:00Z",
            )
        )
        assert failed.output_references == []
        assert store.get(m.Hypothesis, entities["hypothesis"].id) == entities["hypothesis"]
    with StateStore(path) as store:
        assert store.get(m.Task, task.id) == failed
        assert store.history(m.Task, task.id) == [task, repaired, failed]


def test_task_start_is_attached_once(store):
    entities = seed(store)
    pending = store.create(
        changed(entities["task"], id=uuid4(), status="pending", started_at=None, finished_at=None)
    )
    running = store.update(changed(pending, status="running", started_at="2026-09-18T08:00:01Z"))
    with pytest.raises(StateError):
        store.update(changed(running, status="pending", started_at=None))
    assert store.get(m.Task, pending.id) == running
