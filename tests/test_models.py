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
    "baseline_decision": models.Decision,
    "task": models.Task,
    "evidence": models.Evidence,
    "baseline": models.Baseline,
    "baseline_run": models.Run,
    "baseline_experiment": models.Experiment,
}
PROTOCOLS = {
    "coding_task": protocols.CodingTask,
    "coding_result": protocols.CodingResult,
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


@pytest.mark.parametrize("field", ["llm_slots", "coding_slots", "cpu_jobs", "gpu_jobs"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_resource_slots_require_positive_integers(field, value):
    data = payload("project_config")
    data["resources"][field] = value
    with pytest.raises(ValidationError):
        protocols.ProjectConfig.model_validate(data)


def test_three_independent_branches_with_falsification():
    action = protocols.ManagerAction.model_validate(
        payload("manager_action_dispatch_research_agents")
    )
    tasks = action.action.tasks
    assert len(tasks) == 3
    assert len({t.branch_id for t in tasks}) == 3
    assert all(t.context_policy == "independent" for t in tasks)
    assert any(t.perspective == "Falsification" for t in tasks)


@pytest.mark.parametrize("fault", ["duplicate_task", "duplicate_branch", "wrong_project"])
def test_dispatch_rejects_inconsistent_identity(fault):
    data = payload("manager_action_dispatch_research_agents")
    tasks = data["action"]["tasks"]
    if fault == "duplicate_task":
        tasks[1]["id"] = tasks[0]["id"]
    elif fault == "duplicate_branch":
        tasks[1]["branch_id"] = tasks[0]["branch_id"]
    else:
        tasks[1]["project_id"] = str(UUID(int=999))
    with pytest.raises(ValidationError):
        protocols.ManagerAction.model_validate(data)


@pytest.mark.parametrize("target", list(protocols.ProtectedTarget))
def test_all_protected_changes_are_proposals(target):
    data = payload("manager_action_propose_protected_change")
    data["action"]["target"] = target
    assert protocols.ManagerAction.model_validate(data).action.requires_human_approval is True
    data["action"]["requires_human_approval"] = False
    with pytest.raises(ValidationError):
        protocols.ManagerAction.model_validate(data)


def test_boolean_constraint_without_numeric_threshold():
    data = payload("evaluator_result")
    data["constraint_checks"] = [{"name": "correctness", "passed": False}]
    result = protocols.EvaluatorResult.model_validate(data)
    assert result.status == "ok"
    assert result.constraint_checks[0].measured_value is None
    assert_roundtrip(result)


@pytest.mark.parametrize("direction", ["minimize", "maximize", "informational"])
def test_metric_directions(direction):
    data = payload("evaluator_result")
    data["measurements"][0]["direction"] = direction
    assert_roundtrip(protocols.EvaluatorResult.model_validate(data))


def test_invalid_evaluation_cannot_support_observation():
    data = entity("observation")
    data["evaluation"].update(status="invalid", measurements=[])
    with pytest.raises(ValidationError):
        models.Observation.model_validate(data)
    data["relation"] = "invalid"
    assert_roundtrip(models.Observation.model_validate(data))


def test_task_failed_after_one_repair_has_no_scientific_output():
    data = entity("task")
    data.update(
        status="failed", failure="Structured output invalid after repair", repair_attempts=1
    )
    assert_roundtrip(models.Task.model_validate(data))
    for changes in (
        {"repair_attempts": 2},
        {"failure": None},
        {"finished_at": None},
        {"output_references": [{"entity_type": "hypothesis", "entity_id": str(UUID(int=4))}]},
        {"finished_at": "2020-01-01T00:00:00Z"},
    ):
        with pytest.raises(ValidationError):
            models.Task.model_validate(dict(data, **changes))


@pytest.mark.parametrize(
    "status", ["pending", "running", "completed", "failed", "timeout", "cancelled"]
)
def test_task_lifecycle_states(status):
    data = entity("task")
    data["status"] = status
    if status in ("pending", "running"):
        data["finished_at"] = None
    if status in ("failed", "timeout"):
        data["failure"] = "Task did not complete"
    assert_roundtrip(models.Task.model_validate(data))


def test_coding_completion_is_not_execution_success():
    result = protocols.CodingResult.model_validate(payload("coding_result"))
    assert result.status == "implemented"
    with pytest.raises(ValidationError):
        protocols.ExperimentResult.model_validate(result.model_dump())
    with pytest.raises(ValidationError):
        protocols.CodingResult.model_validate(dict(payload("coding_result"), status="failed"))


@pytest.mark.parametrize("fault", ["run", "invalid", "self_replace"])
def test_invalid_baseline(fault):
    data = entity("baseline")
    if fault == "run":
        data["run_id"] = str(UUID(int=999))
    elif fault == "invalid":
        data["evaluation"]["status"] = "invalid"
    else:
        data["previous_baseline_id"] = data["id"]
    with pytest.raises(ValidationError):
        models.Baseline.model_validate(data)


def test_new_status_and_policy_vocabulary():
    from argos.common import ContextPolicy, ExperimentStatus, HypothesisStatus, SubproblemStatus

    for status in ExperimentStatus:
        assert_roundtrip(
            models.Experiment.model_validate(dict(entity("experiment"), status=status))
        )
    for status in HypothesisStatus:
        assert_roundtrip(
            models.Hypothesis.model_validate(dict(entity("hypothesis"), status=status))
        )
    for status in SubproblemStatus:
        assert_roundtrip(
            models.Subproblem.model_validate(dict(entity("subproblem"), status=status))
        )
    for policy in ContextPolicy:
        assert_roundtrip(
            models.ResearchBranch.model_validate(
                dict(entity("research_branch"), context_policy=policy)
            )
        )
    for cls, data, changes in [
        (models.Hypothesis, entity("hypothesis"), {"status": "active"}),
        (models.ResearchBranch, entity("research_branch"), {"context_policy": "public"}),
        (models.Subproblem, entity("subproblem"), {"priority": 2}),
        (models.Hypothesis, entity("hypothesis"), {"confidence": -1}),
    ]:
        with pytest.raises(ValidationError):
            cls.model_validate(dict(data, **changes))


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_coding_failure_roundtrip_and_timeout_consistency(status):
    data = payload("coding_result")
    data.update(
        status=status,
        failure={
            "kind": "timeout" if status == "timeout" else "implementation_failure",
            "message": "Coding did not complete",
        },
    )
    assert_roundtrip(protocols.CodingResult.model_validate(data))
    data["failure"]["kind"] = "implementation_failure" if status == "timeout" else "timeout"
    with pytest.raises(ValidationError):
        protocols.CodingResult.model_validate(data)


def test_baseline_history_keeps_previous_snapshot():
    previous = models.Baseline.model_validate(entity("baseline"))
    original = previous.model_dump_json()
    data = previous.model_dump(mode="json")
    data.update(id=str(UUID(int=100)), previous_baseline_id=str(previous.id))
    refreshed = models.Baseline.model_validate(data)
    assert refreshed.previous_baseline_id == previous.id
    assert previous.model_dump_json() == original
    assert_roundtrip(refreshed)
    run = models.Run.model_validate(entity("baseline_run"))
    experiment = models.Experiment.model_validate(entity("baseline_experiment"))
    assert previous.run_id == run.id
    assert run.experiment_id == experiment.id
    assert previous.source_commit == run.result.source_commit == run.result.resulting_commit
    assert previous.evaluation == run.evaluation
    approval = models.Decision.model_validate(entity("baseline_decision"))
    assert approval.id == previous.approval_decision_id
    assert approval.actor == "human"  # Fixture label only; runtime must authenticate the human.


@pytest.mark.parametrize("field", ["max_cycles", "max_experiments", "stagnation_cycles"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_cycle_limits(field, value):
    data = payload("project_config")
    data["limits"][field] = value
    with pytest.raises(ValidationError):
        protocols.ProjectConfig.model_validate(data)


def test_invalid_metric_direction():
    data = payload("evaluator_result")
    data["measurements"][0]["direction"] = "improve"
    with pytest.raises(ValidationError):
        protocols.EvaluatorResult.model_validate(data)
