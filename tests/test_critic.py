"""Issue #7: blind review, durable verdicts and the review → RM boundary."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from argos import models as m
from argos.backends import FakeLLMBackend
from argos.protocols import CriticReview, ManagerAction
from argos.research import (
    CriticInput,
    CriticState,
    FakeCritic,
    LLMCritic,
    ManagerPlan,
    ResearchManager,
    ResearchStateWriter,
    StructuredCaller,
)
from argos.state import StateError, StateStore
from argos.state.store import TYPES

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "critic.sqlite"
    data = json.loads((EXAMPLES / "entities.json").read_text())
    with StateStore(path) as store:
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
        ):
            store.create(TYPES[name].model_validate(data[name]))
        project = store.list(m.Project)[0]
        store.approve_main_question(
            project.id, project.config.main_research_question, rationale="Human initialization"
        )
        writer = ResearchStateWriter(store)
        adapter = CriticState(store)
        claim = store.list(m.Claim)[0]
        run = store.list(m.Run)[0]
        context = adapter.prepare(claim.id, code_diffs={run.id: "-old\n+new"})
        yield store, writer, adapter, context, path


def review(context, verdict):
    data = json.loads((EXAMPLES / "protocols" / f"critic_review_{verdict}.json").read_text())
    data["claim_id"] = str(context.claim.id)
    return CriticReview.model_validate(data)


@pytest.mark.parametrize("verdict", ["accept", "reject", "needs_more_evidence"])
def test_verdict_persists_and_survives_reopen_without_mutating_science(setup, verdict):
    store, writer, adapter, context, path = setup
    ident = uuid4()
    expected = review(context, verdict)
    before = {cls: store.list(cls) for cls in (m.Project, m.Claim, m.Run, m.Hypothesis)}
    critic = FakeCritic({ident: [expected]}, record_task=writer.record_task)
    outcome = asyncio.run(critic.review(context, task_id=ident))
    assert outcome.task.status == "completed" and outcome.output == expected
    assert store.reviews(context.project_id) == []  # Roles cannot write reviews.
    stored_id = adapter.record(context, outcome)
    with StateStore(path) as reopened:
        saved = reopened.reviews(context.project_id)[0]
        assert saved.id == stored_id and saved.review == expected
        assert saved.claim == context.claim
        assert reopened.get(m.Task, ident).status == "completed"
        assert reopened.snapshot(context.project_id).reviews == [saved]
        for cls, records in before.items():
            assert reopened.list(cls) == records


def test_blind_projection_omits_private_state_and_contains_raw_evidence(setup):
    store, _, adapter, context, _ = setup
    hypothesis = store.list(m.Hypothesis)[0]
    hypothesis.rationale = "PRIVATE-HYPOTHESIS-REASONING"
    store.update(hypothesis)
    decision = m.Decision.model_validate(
        json.loads((EXAMPLES / "entities.json").read_text())["decision"]
    )
    decision.id = uuid4()
    decision.rationale = "PRIVATE-RM-REASONING"
    store.create(decision)
    context = adapter.prepare(
        context.claim.id, code_diffs={context.evidence[0].result.run_id: "-old\n+new"}
    )
    task = uuid4()
    critic = FakeCritic({task: [review(context, "reject")]})
    asyncio.run(critic.review(context, task_id=task))
    request = critic.backend.requests[0]
    assert request.role == "critic"
    assert "PRIVATE-" not in request.context_json
    assert "rationale" not in request.context_json
    sent = json.loads(request.context_json)
    assert set(sent) == {
        "project_id",
        "main_research_question",
        "held_out_test_protocol",
        "claim",
        "evidence",
    }
    item = sent["evidence"][0]
    assert item["spec"]["hypothesis_statement"] == hypothesis.statement
    assert item["result"]["commands"] and item["evaluation"]["measurements"]
    assert item["code_diff"] == "-old\n+new" and item["relation"] == "contradicts"
    assert "alternative explanations" in request.system_prompt
    with pytest.raises(ValidationError):
        CriticInput.model_validate({**sent, "rm_private_reasoning": "do not forward"})


@pytest.mark.parametrize("bad", ["json", "claim_id", "rationale", "checks", "verdict"])
def test_invalid_output_repairs_once_and_never_publishes_review(setup, bad):
    store, writer, adapter, context, path = setup
    data = review(context, "needs_more_evidence").model_dump(mode="json")
    if bad == "claim_id":
        data["claim_id"] = str(uuid4())
    elif bad == "rationale":
        data["rationale"] = "  "
    elif bad == "checks":
        data["requested_checks"] = []
    elif bad == "verdict":
        data["verdict"] = "probably_accept"
    raw = "not JSON" if bad == "json" else json.dumps(data)
    ident = uuid4()
    critic = FakeCritic(
        {ident: [raw, raw, review(context, "accept")]}, record_task=writer.record_task
    )
    outcome = asyncio.run(critic.review(context, task_id=ident))
    assert outcome.task.status == "failed" and outcome.output is None
    assert outcome.task.repair_attempts == 1
    assert len(critic.backend.requests) == 2
    assert critic.backend.requests[1].repair_error
    assert adapter.record(context, outcome) is None
    with StateStore(path) as reopened:
        assert reopened.reviews(context.project_id) == []
        assert reopened.get(m.Task, ident).status == "failed"
    assert store.get(m.Claim, context.claim.id) == context.claim


def test_repair_can_succeed_and_preserves_same_blind_context(setup):
    _, writer, adapter, context, _ = setup
    ident = uuid4()
    critic = FakeCritic({ident: ["{}", review(context, "accept")]}, record_task=writer.record_task)
    outcome = asyncio.run(critic.review(context, task_id=ident))
    assert outcome.task.status == "completed" and outcome.task.repair_attempts == 1
    assert critic.backend.requests[0].context_json == critic.backend.requests[1].context_json
    assert adapter.record(context, outcome)


@pytest.mark.parametrize("change", ["claim", "subproblem"])
def test_revised_claim_or_evidence_cannot_receive_stale_review(setup, change):
    store, _, adapter, context, _ = setup
    ident = uuid4()
    outcome = asyncio.run(
        FakeCritic({ident: [review(context, "accept")]}).review(context, task_id=ident)
    )
    if change == "claim":
        entity = store.get(m.Claim, context.claim.id)
        entity.statement = "Broader claim than the reviewer saw"
    else:
        entity = store.list(m.Subproblem)[0]
        entity.question = "Revised relevant research question"
    store.update(entity)
    with pytest.raises(StateError, match="changed"):
        adapter.record(context, outcome)
    assert store.reviews(context.project_id) == []


def test_input_cannot_drop_counterevidence_or_mix_run_identity(setup):
    _, _, _, context, _ = setup
    data = context.model_dump(mode="json")
    data["evidence"][0]["relation"] = "supports"
    with pytest.raises(ValidationError, match="all supporting and contradicting"):
        CriticInput.model_validate(data)
    data = context.model_dump(mode="json")
    data["evidence"][0]["evaluation"]["run_id"] = str(uuid4())
    with pytest.raises(ValidationError, match="match the reviewed run"):
        CriticInput.model_validate(data)
    data = context.model_dump(mode="json")
    data["evidence"].append(data["evidence"][0])
    with pytest.raises(ValidationError, match="Duplicate"):
        CriticInput.model_validate(data)


def test_missing_diff_and_unapproved_project_fail_before_review(setup):
    store, _, adapter, context, _ = setup
    with pytest.raises(StateError, match="Missing code diff"):
        adapter.prepare(context.claim.id, code_diffs={})
    project = store.get(m.Project, context.project_id)
    project.status = "paused"
    store.update(project)
    with pytest.raises(StateError, match="active project"):
        adapter.prepare(context.claim.id, code_diffs={})


def test_backend_error_timeout_and_context_budget_publish_no_review(setup):
    store, writer, adapter, context, _ = setup

    class SlowBackend:
        async def complete(self, request):
            await asyncio.sleep(1)

    critics = [
        LLMCritic(
            StructuredCaller(SlowBackend(), timeout_seconds=0.01, record_task=writer.record_task)
        ),
        LLMCritic(StructuredCaller(FakeLLMBackend({}), record_task=writer.record_task)),
        FakeCritic({}, max_context_chars=10, record_task=writer.record_task),
    ]
    for critic, status in zip(critics, ["timeout", "failed", "failed"], strict=True):
        outcome = asyncio.run(critic.review(context, task_id=uuid4()))
        assert outcome.task.status == status and outcome.output is None
        assert adapter.record(context, outcome) is None
    assert not store.reviews(context.project_id)


def test_rm_receives_structured_additional_checks_after_reopen(setup):
    store, writer, adapter, context, path = setup
    ident, manager_id = uuid4(), uuid4()
    expected = review(context, "needs_more_evidence")
    outcome = asyncio.run(
        FakeCritic({ident: [expected]}, record_task=writer.record_task).review(
            context, task_id=ident
        )
    )
    adapter.record(context, outcome)
    next_action = ManagerAction(
        project_id=context.project_id,
        rationale="Critic identified a missing controlled check",
        action={"action_type": "continue_research", "next_question": expected.requested_checks[0]},
    )
    backend = FakeLLMBackend(
        {
            manager_id: [
                ManagerPlan(
                    summary="Gather requested evidence before treating the claim as accepted",
                    actions=[next_action],
                ).model_dump_json()
            ]
        }
    )
    with StateStore(path) as reopened:
        result = asyncio.run(
            ResearchManager(StructuredCaller(backend)).plan(
                reopened.snapshot(context.project_id), task_id=manager_id, event="review_recorded"
            )
        )
    assert result.output.actions == [next_action]
    sent = json.loads(backend.requests[0].context_json)
    assert sent["event"] == "review_recorded"
    assert sent["frontier"]["reviews"][0]["review"]["requested_checks"] == expected.requested_checks
    assert "needs_more_evidence as unresolved" in backend.requests[0].system_prompt
    assert store.get(m.Claim, context.claim.id) == context.claim


def test_projection_keeps_both_evidence_sides_and_exact_claim_scope(setup):
    store, _, adapter, context, _ = setup
    observation = store.list(m.Observation)[0].model_copy(deep=True)
    observation.id = uuid4()
    observation.summary = "A second recorded observation relevant to this claim"
    store.create(observation)
    claim = store.get(m.Claim, context.claim.id)
    claim.evidence.supporting_observation_ids.append(observation.id)
    claim.statement = "Only for the measured configuration, the result has this effect"
    store.update(claim)
    projected = adapter.prepare(claim.id, code_diffs={context.evidence[0].result.run_id: ""})
    assert projected.claim.statement == claim.statement
    assert {e.relation for e in projected.evidence} == {"supports", "contradicts"}
    assert {e.observation_id for e in projected.evidence} == {
        observation.id,
        context.evidence[0].observation_id,
    }
    assert all(e.code_diff == "" for e in projected.evidence)


def test_persistence_revalidates_outcome_and_rejects_foreign_identity(setup):
    store, _, adapter, context, _ = setup
    ident = uuid4()
    outcome = asyncio.run(
        FakeCritic({ident: [review(context, "accept")]}).review(context, task_id=ident)
    )
    foreign = outcome.model_copy(deep=True)
    foreign.task.project_id = uuid4()
    with pytest.raises(StateError, match="project's Critic task"):
        adapter.record(context, foreign)
    wrong_claim = outcome.model_copy(deep=True)
    wrong_claim.output.claim_id = uuid4()
    with pytest.raises(StateError, match="assigned claim"):
        adapter.record(context, wrong_claim)
    invalid = outcome.model_copy(deep=True)
    invalid.output = invalid.output.model_copy(update={"rationale": ""})
    with pytest.raises(ValidationError):
        adapter.record(context, invalid)
    assert not store.reviews(context.project_id)
