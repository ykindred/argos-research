"""Public payload contracts, provenance, and failure semantics."""

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from argos import models, protocols
from argos.common import ExecutionFailureType, Model

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ENTITIES = {
    "project": models.Project,
    "subproblem": models.Subproblem,
    "research_branch": models.ResearchBranch,
    "hypothesis": models.Hypothesis,
    "experiment": models.Experiment,
    "run": models.Run,
    "observation": models.Observation,
    "claim": models.Claim,
    "decision": models.Decision,
}
PROTOCOLS = {
    "project_config": protocols.ProjectConfig,
    "research_task": protocols.ResearchTask,
    "research_agent_result": protocols.ResearchAgentResult,
    "experiment_spec": protocols.ExperimentSpec,
    "experiment_result": protocols.ExperimentResult,
    "evaluator_result": protocols.EvaluatorResult,
    "critic_review": protocols.CriticReview,
    "manager_action": protocols.ManagerAction,
}


def payload(name):
    return json.loads((EXAMPLES / "protocols" / f"{name}.json").read_text())


def entity(name):
    return json.loads((EXAMPLES / "entities.json").read_text())[name]


def examples():
    for name, cls in ENTITIES.items():
        yield pytest.param(cls, entity(name), id=name)
    for path in sorted((EXAMPLES / "protocols").glob("*.json")):
        cls = next(cls for prefix, cls in PROTOCOLS.items() if path.stem.startswith(prefix))
        yield pytest.param(cls, json.loads(path.read_text()), id=path.stem)


CASES = list(examples())


def assert_roundtrip(value):
    if isinstance(value, Model):
        cls = type(value)
        assert cls.model_validate_json(value.model_dump_json()) == value
        assert cls.model_json_schema()
        for name in cls.model_fields:
            assert_roundtrip(getattr(value, name))
    elif isinstance(value, list):
        for item in value:
            assert_roundtrip(item)


@pytest.mark.parametrize("cls,data", CASES)
def test_all_examples_and_nested_models_roundtrip(cls, data):
    assert_roundtrip(cls.model_validate(data))


@pytest.mark.parametrize("cls,data", CASES)
def test_missing_required_fields_and_unexpected_fields_rejected(cls, data):
    for name, field in cls.model_fields.items():
        if field.is_required():
            invalid = {key: value for key, value in data.items() if key != name}
            with pytest.raises(ValidationError):
                cls.model_validate(invalid)
    with pytest.raises(ValidationError):
        cls.model_validate(dict(data, private_reasoning="Not a protocol field"))


@pytest.mark.parametrize("name", ENTITIES)
def test_stable_entity_ids_and_aware_timestamps(name):
    cls = ENTITIES[name]
    data = entity(name)
    instance = cls.model_validate(data)
    assert isinstance(instance.id, UUID)
    assert cls.model_validate_json(instance.model_dump_json()).id == instance.id
    for key, invalid in [("id", "not-an-id"), ("created_at", "2026-09-18T08:00:00")]:
        with pytest.raises(ValidationError):
            cls.model_validate(dict(data, **{key: invalid}))


@pytest.mark.parametrize(
    "name", ["project", "subproblem", "research_branch", "hypothesis", "experiment", "run", "claim"]
)
def test_invalid_entity_status(name):
    with pytest.raises(ValidationError):
        ENTITIES[name].model_validate(dict(entity(name), status="invented"))


@pytest.mark.parametrize("verdict", ["approve", "ACCEPT", "", None])
def test_invalid_critic_verdict(verdict):
    with pytest.raises(ValidationError):
        protocols.CriticReview.model_validate(
            dict(payload("critic_review_accept"), verdict=verdict)
        )


def test_every_action_has_a_typed_example():
    actions = [
        protocols.ManagerAction.model_validate_json(path.read_text()).action.action_type
        for path in (EXAMPLES / "protocols").glob("manager_action_*.json")
    ]
    assert set(actions) == set(protocols.ManagerActionType)


@pytest.mark.parametrize(
    "action",
    [
        {"action_type": "execute_shell", "command": "anything"},
        {"action_type": "select_hypothesis"},
        {"action_type": "create_subproblem", "question": "   "},
        {"action_type": "dispatch_research_agents", "tasks": []},
        {"action_type": "select_hypothesis", "hypothesis_id": "bad-id"},
        {
            "action_type": "propose_main_question_revision",
            "proposed_question": "New question",
            "requires_human_approval": False,
        },
        {"action_type": "update_main_research_question", "question": "New question"},
    ],
)
def test_invalid_actions_and_unapproved_question_change(action):
    data = payload("manager_action_create_subproblem")
    with pytest.raises(ValidationError):
        protocols.ManagerAction.model_validate(dict(data, action=action))


def test_revision_is_only_a_proposal():
    project = models.Project.model_validate(entity("project"))
    original = project.model_dump_json()
    action = protocols.ManagerAction.model_validate(
        payload("manager_action_propose_main_question_revision")
    )
    assert action.action.requires_human_approval is True
    assert project.model_dump_json() == original


def test_negative_scientific_result_is_successful_execution():
    run = models.Run.model_validate(entity("run"))
    claim = models.Claim.model_validate(entity("claim"))
    assert run.result.status == "succeeded"
    assert run.result.failure is None
    assert run.evaluation.measurements[0].value > 10
    assert run.evaluation.constraint_checks[0].passed is False
    assert claim.evidence.contradicting_observation_ids


@pytest.mark.parametrize("kind", list(ExecutionFailureType))
def test_all_execution_failures_can_be_recorded(kind):
    data = payload("experiment_result_failure")
    data["failure"]["kind"] = kind
    result = protocols.ExperimentResult.model_validate(data)
    run = models.Run(
        id=result.run_id,
        created_at=result.started_at,
        experiment_id=result.experiment_id,
        status="failed",
        result=result,
    )
    assert_roundtrip(run)
    assert run.evaluation is None


@pytest.mark.parametrize(
    "change",
    [
        {"failure": {"kind": "timeout", "message": "Timed out"}},
        {"status": "failed"},
        {"status": "running"},
        {"resulting_commit": None},
        {"commands": []},
        {"finished_at": "2026-09-17T08:00:00Z"},
        {"commands": [{"argv": ["python"], "exit_code": 1, "stdout": "", "stderr": "error"}]},
    ],
)
def test_inconsistent_execution_results_rejected(change):
    with pytest.raises(ValidationError):
        protocols.ExperimentResult.model_validate(
            dict(payload("experiment_result_success"), **change)
        )


@pytest.mark.parametrize(
    "change",
    [
        {"result": None},
        {"status": "failed"},
        {"status": "running"},
        {"id": "00000000-0000-4000-8000-000000000099"},
        {"experiment_id": "00000000-0000-4000-8000-000000000099"},
    ],
)
def test_inconsistent_run_rejected(change):
    with pytest.raises(ValidationError):
        models.Run.model_validate(dict(entity("run"), **change))


def test_failed_run_cannot_supply_measurements():
    data = entity("run")
    data.update(status="failed", result=payload("experiment_result_failure"))
    data["result"]["run_id"] = data["id"]
    with pytest.raises(ValidationError):
        models.Run.model_validate(data)


def test_pending_run_has_no_fabricated_execution_record():
    data = entity("run")
    data.update(status="pending", result=None, evaluation=None)
    assert_roundtrip(models.Run.model_validate(data))


@pytest.mark.parametrize(
    "name,field", [("experiment", "id"), ("observation", "run_id"), ("decision", "project_id")]
)
def test_nested_reference_mismatch(name, field):
    data = entity(name)
    data[field] = "00000000-0000-4000-8000-000000000099"
    with pytest.raises(ValidationError):
        ENTITIES[name].model_validate(data)


def test_provenance_chain_example():
    state = {name: cls.model_validate(entity(name)) for name, cls in ENTITIES.items()}
    assert state["subproblem"].project_id == state["project"].id
    assert state["hypothesis"].subproblem_id == state["subproblem"].id
    assert state["research_branch"].subproblem_id == state["subproblem"].id
    assert state["hypothesis"].branch_id == state["research_branch"].id
    assert state["experiment"].spec.hypothesis_id == state["hypothesis"].id
    assert state["run"].experiment_id == state["experiment"].id
    assert state["observation"].run_id == state["run"].id
    assert state["claim"].evidence.contradicting_observation_ids == [state["observation"].id]


@pytest.mark.parametrize("support,contradict", [([], []), ([7], [7]), ([7, 7], []), ([], [7, 7])])
def test_invalid_claim_evidence(support, contradict):
    def ids(numbers):
        return [str(UUID(int=n)) for n in numbers]

    with pytest.raises(ValidationError):
        protocols.ClaimEvidence(
            supporting_observation_ids=ids(support), contradicting_observation_ids=ids(contradict)
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_measurements_rejected(value):
    data = payload("evaluator_result")
    data["measurements"][0]["value"] = value
    with pytest.raises(ValidationError):
        protocols.EvaluatorResult.model_validate(data)


def test_evaluator_rejects_interpretation_and_duplicate_measurements():
    data = payload("evaluator_result")
    with pytest.raises(ValidationError):
        protocols.EvaluatorResult.model_validate(dict(data, hypothesis_is_correct=True))
    data["measurements"] *= 2
    with pytest.raises(ValidationError):
        protocols.EvaluatorResult.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
        ("max_memory_mb", -1),
        ("max_cpu_cores", 0),
    ],
)
def test_invalid_resource_limits(field, value):
    data = payload("experiment_spec")
    data["resource_limits"][field] = value
    with pytest.raises(ValidationError):
        protocols.ExperimentSpec.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [("run_steps", []), ("run_steps", [[]]), ("build_steps", [[]]), ("test_steps", [[" "]])],
)
def test_invalid_execution_steps(field, value):
    with pytest.raises(ValidationError):
        protocols.ExperimentSpec.model_validate(dict(payload("experiment_spec"), **{field: value}))
