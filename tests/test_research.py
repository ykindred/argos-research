"""Issue #4: fake reasoning flow, isolation, failure data and human authority."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from argos import models as m
from argos.backends import FakeLLMBackend
from argos.protocols import ManagerAction
from argos.research import (
    ManagerPlan,
    ResearchAgent,
    ResearchDispatcher,
    ResearchManager,
    ResearchStateWriter,
    StructuredCaller,
    TaskRecordingError,
)
from argos.state import StateError, StateStore

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def payload(name):
    return json.loads((EXAMPLES / "protocols" / f"{name}.json").read_text())


def action(project, kind, **fields):
    return ManagerAction.model_validate(
        {
            "project_id": project.id,
            "rationale": "Choose a cheap decisive test",
            "action": {"action_type": kind, **fields},
        }
    )


def plan(*actions):
    return ManagerPlan(
        summary="Agreement on mechanism; uncertainty requires a pilot", actions=list(actions)
    )


def reply(task, marker="isolated result"):
    data = payload("research_agent_result")
    data.update(task_id=str(task.id), agent_id=f"ra:{task.branch_id}", summary=marker)
    data["ideas"][0]["falsification_suggestions"] = ["Test without repeated inputs"]
    return json.dumps(data)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "research.sqlite"
    with StateStore(path) as store:
        entities = json.loads((EXAMPLES / "entities.json").read_text())
        project = store.create(m.Project.model_validate(entities["project"]))
        project = store.approve_main_question(
            project.id,
            project.config.main_research_question,
            rationale="Human initialization",
        )
        store.create(m.Subproblem.model_validate(entities["subproblem"]))
        writer = ResearchStateWriter(store)
        dispatch = ManagerAction.model_validate(payload("manager_action_dispatch_research_agents"))
        yield store, writer, project, dispatch, path


def test_fake_rm_three_ra_synthesis_hypothesis_selection_experiment(setup):
    store, writer, project, dispatch, path = setup
    initial_id, synthesis_id, experiment_id = uuid4(), uuid4(), uuid4()
    tasks = dispatch.action.tasks
    create = action(
        project,
        "create_hypothesis",
        subproblem_id=tasks[0].subproblem_id,
        branch_id=tasks[0].branch_id,
        statement="Caching reduces latency below 10 ms.",
        rationale="Three independent proposals motivate a controlled pilot",
    )
    responses = {t.id: [reply(t, f"PRIVATE-OUTPUT-{i}")] for i, t in enumerate(tasks)}
    responses.update(
        {
            initial_id: [plan(dispatch).model_dump_json()],
            synthesis_id: [plan(create).model_dump_json()],
        }
    )
    backend = FakeLLMBackend(responses)
    caller = StructuredCaller(backend, llm_slots=2, record_task=writer.record_task)
    manager = ResearchManager(caller)

    async def run():
        initial = await manager.plan(
            store.snapshot(project.id), task_id=initial_id, event="initialized"
        )
        assert initial.task.status == "completed"
        # Roles propose only; they do not apply scientific entities.
        assert store.list(m.ResearchBranch) == []
        branches = writer.apply(initial.output.actions[0], cycle=1)
        assert len(branches) == 3
        batch = await ResearchDispatcher(ResearchAgent(caller)).dispatch(initial.output.actions[0])
        assert len(batch.completed_results) == 3
        ra_requests = [r for r in backend.requests if r.role == "research_agent"]
        for request, task in zip(ra_requests, tasks, strict=True):
            context = json.loads(request.context_json)
            assert context["task"] == task.model_dump(mode="json")
            assert "PRIVATE-OUTPUT" not in request.context_json
            assert "frontier" not in context
        assert "Falsification mode" in ra_requests[2].system_prompt
        synthesized = await manager.synthesize(
            store.snapshot(project.id), batch, task_id=synthesis_id
        )
        assert synthesized.task.status == "completed"
        synthesis_context = json.loads(backend.requests[-1].context_json)
        assert len(synthesis_context["exploration"]["outcomes"]) == 3
        assert all(f"PRIVATE-OUTPUT-{i}" in backend.requests[-1].context_json for i in range(3))
        hypothesis = writer.apply(synthesized.output.actions[0], cycle=1)
        spec = payload("experiment_spec")
        spec.update(hypothesis_id=str(hypothesis.id), experiment_id=str(uuid4()))
        select = action(project, "select_hypothesis", hypothesis_id=hypothesis.id)
        experiment = action(project, "propose_experiment", spec=spec)
        # Script the next event after the host has assigned and persisted the hypothesis ID.
        from collections import deque

        backend.responses[experiment_id] = deque([plan(select, experiment).model_dump_json()])
        proposal = await manager.plan(
            store.snapshot(project.id), task_id=experiment_id, event="hypotheses_recorded"
        )
        assert proposal.task.status == "completed"
        for proposed in proposal.output.actions:
            writer.apply(proposed, cycle=1)
        assert store.get(m.Hypothesis, hypothesis.id).status == "proposed"
        assert store.list(m.Experiment)[0].spec.hypothesis_id == hypothesis.id
        assert len(store.list(m.Task)) == 6
        assert all(t.status == "completed" for t in store.list(m.Task))
        assert store.get(m.Project, project.id) == project

    asyncio.run(run())
    with StateStore(path) as reopened:
        assert len(reopened.list(m.Hypothesis)) == 1
        assert len(reopened.list(m.Experiment)) == 1
        assert len(reopened.list(m.ResearchBranch)) == 3
        assert len(reopened.list(m.Decision)) == 5  # initial approval and four actions


def test_invalid_ra_repair_is_once_durable_and_siblings_survive(setup):
    store, writer, project, dispatch, path = setup
    writer.apply(dispatch, cycle=1)
    tasks = dispatch.action.tasks
    backend = FakeLLMBackend(
        {
            tasks[0].id: ["not JSON", "still invalid", reply(tasks[0])],
            tasks[1].id: ["{}", reply(tasks[1])],
            tasks[2].id: [reply(tasks[2])],
        }
    )
    batch = asyncio.run(
        ResearchDispatcher(
            ResearchAgent(
                StructuredCaller(
                    backend,
                    record_task=writer.record_task,
                )
            )
        ).dispatch(dispatch)
    )
    assert [o.task.status for o in batch.outcomes] == ["failed", "completed", "completed"]
    assert [o.task.repair_attempts for o in batch.outcomes] == [1, 1, 0]
    assert len(batch.completed_results) == 2
    assert len(backend.responses[tasks[0].id]) == 1
    assert len(backend.requests) == 5
    assert store.list(m.Hypothesis) == []
    failed_history = store.history(m.Task, tasks[0].id)
    assert [t.repair_attempts for t in failed_history] == [0, 1, 1]
    assert failed_history[-1].output_references == []
    with StateStore(path) as reopened:
        assert reopened.get(m.Task, tasks[0].id).status == "failed"
    synth_id = uuid4()
    rm_backend = FakeLLMBackend(
        {
            synth_id: [
                plan(
                    action(project, "continue_research", next_question="Missing evidence")
                ).model_dump_json()
            ]
        }
    )
    outcome = asyncio.run(
        ResearchManager(StructuredCaller(rm_backend)).synthesize(
            store.snapshot(project.id),
            batch,
            task_id=synth_id,
        )
    )
    assert outcome.task.status == "completed"
    inputs = json.loads(rm_backend.requests[0].context_json)["exploration"]["outcomes"]
    assert inputs[0]["task"]["failure"] and inputs[0]["output"] is None
    assert len(inputs) == 3


@pytest.mark.parametrize("approve", [True, False])
def test_main_question_gate_is_proposal_only_and_requires_explicit_human(setup, approve):
    store, writer, project, _, path = setup
    ident = uuid4()
    proposal = action(
        project, "propose_main_question_revision", proposed_question="Narrower question?"
    )
    backend = FakeLLMBackend({ident: [plan(proposal).model_dump_json()]})
    result = asyncio.run(
        ResearchManager(StructuredCaller(backend)).plan(
            store.snapshot(project.id),
            task_id=ident,
            event="initialized",
        )
    )
    assert store.get(m.Project, project.id) == project
    writer.apply(result.output.actions[0], cycle=1)
    assert (
        store.snapshot(project.id).main_research_question == project.config.main_research_question
    )
    with StateStore(path) as reopened:
        assert reopened.get(m.Project, project.id).status == "paused"
    with pytest.raises(StateError):
        writer.apply(
            action(project, "create_subproblem", question="Premature continuation"), cycle=2
        )
    with pytest.raises(StateError):
        writer.answer_main_question(
            project.id,
            proposed_question="Stale wording",
            approve=True,
            rationale="Human input",
            cycle=2,
        )
    result = writer.answer_main_question(
        project.id,
        proposed_question="Narrower question?",
        approve=approve,
        rationale="Explicit human answer",
        cycle=2,
    )
    assert result.config.main_research_question == (
        "Narrower question?" if approve else project.config.main_research_question
    )
    assert result.status == "active" and result.proposed_main_research_question is None
    assert store.list(m.Decision)[0].actor == "human"
    with pytest.raises(StateError):
        writer.answer_main_question(
            project.id,
            proposed_question="Narrower question?",
            approve=True,
            rationale="Cannot replay",
            cycle=3,
        )


def test_invalid_manager_plan_is_atomic_and_semantic_checks_repair(setup):
    store, writer, project, dispatch, _ = setup
    ident = uuid4()
    bad = plan(dispatch).model_dump(mode="json")
    bad["actions"].append(
        {
            "project_id": str(project.id),
            "rationale": "bad",
            "action": {"action_type": "execute_shell"},
        }
    )
    backend = FakeLLMBackend({ident: [json.dumps(bad), json.dumps(bad)]})
    result = asyncio.run(
        ResearchManager(StructuredCaller(backend, record_task=writer.record_task)).plan(
            store.snapshot(project.id),
            task_id=ident,
            event="initialized",
        )
    )
    assert result.output is None and result.task.status == "failed"
    assert not store.list(m.ResearchBranch)
    assert not store.list(m.Hypothesis)
    assert len(backend.requests) == 2


@pytest.mark.parametrize(
    "change", ["project", "question", "evaluator", "baseline", "scope", "resources"]
)
def test_manager_rejects_cross_project_and_protected_changes(setup, change):
    store, _, project, dispatch, _ = setup
    if change in ("project", "question"):
        bad = dispatch.model_dump(mode="json")
        if change == "project":
            bad = action(project, "stop", reason="End").model_dump(mode="json")
            bad["project_id"] = str(uuid4())
        else:
            bad["action"]["tasks"][0]["main_research_question"] = "Unapproved question"
    else:
        bad = payload("manager_action_propose_experiment")
        spec = bad["action"]["spec"]
        if change == "evaluator":
            spec["evaluation_protocol"]["command"] = ["replace-evaluator"]
        elif change == "baseline":
            spec["baseline_id"] = str(uuid4())
        elif change == "scope":
            spec["scope"] = None
        else:
            spec["resource_limits"]["timeout_seconds"] = 999999
    ident = uuid4()
    raw = json.dumps({"summary": "invalid", "actions": [bad]})
    backend = FakeLLMBackend({ident: [raw, raw]})
    result = asyncio.run(
        ResearchManager(StructuredCaller(backend)).plan(
            store.snapshot(project.id),
            task_id=ident,
            event="initialized",
        )
    )
    assert result.task.status == "failed" and result.task.repair_attempts == 1


def test_result_identity_is_validated_and_repaired(setup):
    _, _, _, dispatch, _ = setup
    task = dispatch.action.tasks[0]
    bad = json.loads(reply(task))
    bad["task_id"] = str(uuid4())
    backend = FakeLLMBackend({task.id: [json.dumps(bad), reply(task)]})
    result = asyncio.run(ResearchAgent(StructuredCaller(backend)).explore(task))
    assert result.output.task_id == task.id
    assert result.task.repair_attempts == 1
    assert "Result must match" in backend.requests[1].repair_error


def test_backend_failure_and_timeout_do_not_cancel_siblings(setup):
    _, _, _, dispatch, _ = setup
    tasks = dispatch.action.tasks

    class Backend:
        async def complete(self, request):
            if request.task_id == tasks[0].id:
                raise RuntimeError("provider unavailable")
            if request.task_id == tasks[1].id:
                await asyncio.sleep(1)
            return reply(next(t for t in tasks if t.id == request.task_id))

    result = asyncio.run(
        ResearchDispatcher(
            ResearchAgent(
                StructuredCaller(
                    Backend(),
                    timeout_seconds=0.03,
                )
            )
        ).dispatch(dispatch)
    )
    assert [o.task.status for o in result.outcomes] == ["failed", "timeout", "completed"]
    assert all(o.task.repair_attempts == 0 for o in result.outcomes)


def test_llm_semaphore_limits_calls_and_contexts_are_not_mutated(setup):
    _, _, _, dispatch, _ = setup
    tasks = dispatch.action.tasks
    active = peak = 0

    class Backend:
        async def complete(self, request):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0.01)
            active -= 1
            result = reply(next(t for t in tasks if t.id == request.task_id))
            request.context_json = "mutated backend copy"
            return result

    before = dispatch.model_dump_json()
    result = asyncio.run(
        ResearchDispatcher(
            ResearchAgent(
                StructuredCaller(
                    Backend(),
                    llm_slots=2,
                )
            )
        ).dispatch(dispatch)
    )
    assert peak == 2 and len(result.completed_results) == 3
    assert dispatch.model_dump_json() == before


def test_persistence_failure_during_repair_propagates_before_second_call(setup):
    _, _, _, dispatch, _ = setup
    task = dispatch.action.tasks[0]
    backend = FakeLLMBackend({task.id: ["invalid", reply(task)]})

    def record(record):
        if record.repair_attempts:
            raise OSError("disk full")

    with pytest.raises(TaskRecordingError):
        asyncio.run(ResearchAgent(StructuredCaller(backend, record_task=record)).explore(task))
    assert len(backend.requests) == 1


def test_cancellation_is_recorded_and_propagates(setup):
    _, _, _, dispatch, _ = setup
    records = []

    class Backend:
        async def complete(self, request):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            ResearchAgent(StructuredCaller(Backend(), record_task=records.append)).explore(
                dispatch.action.tasks[0],
            )
        )
    assert records[-1].status == "cancelled" and records[-1].finished_at


def test_bounded_context_fails_without_model_call_and_does_not_truncate_charter(setup):
    store, _, project, _, _ = setup
    backend = FakeLLMBackend({})
    outcome = asyncio.run(
        ResearchManager(StructuredCaller(backend, max_context_chars=10)).plan(
            store.snapshot(project.id),
            task_id=uuid4(),
            event="initialized",
        )
    )
    assert outcome.task.status == "failed" and "budget" in outcome.task.failure
    assert not backend.requests


def test_dispatch_rejects_shared_context_and_duplicate_ids_before_calls(setup):
    _, _, _, dispatch, _ = setup
    data = dispatch.model_dump(mode="json")
    data["action"]["tasks"][1]["id"] = data["action"]["tasks"][0]["id"]
    with pytest.raises(ValidationError):
        ManagerAction.model_validate(data)
    dispatch.action.tasks[0].context_policy = "shared"
    backend = FakeLLMBackend({})
    with pytest.raises(ValueError, match="independent"):
        asyncio.run(ResearchDispatcher(ResearchAgent(StructuredCaller(backend))).dispatch(dispatch))
    assert not backend.requests


def test_state_adapter_rolls_back_bad_batch_and_records_priorities(setup):
    store, writer, project, dispatch, _ = setup
    dispatch.action.tasks[-1].subproblem_id = uuid4()
    with pytest.raises(KeyError):
        writer.apply(dispatch, cycle=1)
    assert not store.list(m.ResearchBranch)
    assert len(store.list(m.Decision)) == 1
    subproblem = writer.apply(
        action(project, "create_subproblem", question="Cheap check", priority=0.9), cycle=1
    )
    assert subproblem.priority == 0.9
    updated = writer.apply(
        action(
            project,
            "update_subproblem",
            subproblem_id=subproblem.id,
            question="More precise check",
            priority=0.8,
        ),
        cycle=1,
    )
    assert updated.priority == 0.8
    with pytest.raises(NotImplementedError):
        writer.apply(action(project, "run_experiment", experiment_id=uuid4()), cycle=1)


def test_manager_requires_approved_active_project_and_completed_event(setup):
    store, writer, project, _, _ = setup
    backend = FakeLLMBackend({})
    manager = ResearchManager(StructuredCaller(backend))
    snapshot = store.snapshot(project.id)
    with pytest.raises(ValueError, match="completed research event"):
        asyncio.run(manager.plan(snapshot, task_id=uuid4(), event="poll"))
    snapshot.project.main_question_approved = False
    with pytest.raises(ValueError, match="Human must approve"):
        asyncio.run(manager.plan(snapshot, task_id=uuid4(), event="initialized"))
    writer.apply(
        action(project, "propose_main_question_revision", proposed_question="New?"), cycle=1
    )
    with pytest.raises(ValueError, match="not active"):
        asyncio.run(manager.plan(store.snapshot(project.id), task_id=uuid4(), event="initialized"))
    assert not backend.requests


def test_briefing_bounds_frontier_preserves_charter_and_omits_old_action_contexts(setup):
    store, writer, project, _, _ = setup
    for i in range(12):
        writer.apply(action(project, "create_subproblem", question=f"Question {i}"), cycle=i)
    ident = uuid4()
    backend = FakeLLMBackend(
        {ident: [plan(action(project, "stop", reason="Budget exhausted")).model_dump_json()]}
    )
    outcome = asyncio.run(
        ResearchManager(StructuredCaller(backend)).plan(
            store.snapshot(project.id),
            task_id=ident,
            event="initialized",
        )
    )
    assert outcome.task.status == "completed"
    context = json.loads(backend.requests[0].context_json)
    assert len(context["frontier"]["active_subproblems"]) == 8
    assert len(context["frontier"]["decisions"]) == 5
    assert {"active_subproblems", "decisions"} <= set(context["frontier"]["truncated"])
    assert context["project"]["config"]["research_charter"] == project.config.research_charter
    assert all("action" not in d for d in context["frontier"]["decisions"])


def test_rm_can_consume_observations_form_claim_and_request_review_without_changing_metrics(setup):
    store, writer, project, _, _ = setup
    entities = json.loads((EXAMPLES / "entities.json").read_text())
    for name, cls in (
        ("research_branch", m.ResearchBranch),
        ("hypothesis", m.Hypothesis),
        ("experiment", m.Experiment),
        ("run", m.Run),
        ("observation", m.Observation),
        ("claim", m.Claim),
    ):
        store.create(cls.model_validate(entities[name]))
    observation = store.list(m.Observation)[0]
    claim = store.list(m.Claim)[0]
    ident = uuid4()
    form = action(
        project,
        "form_claim",
        statement="Scoped candidate based on measured evidence",
        evidence={
            "supporting_observation_ids": [],
            "contradicting_observation_ids": [observation.id],
        },
    )
    review = action(project, "request_critic_review", claim_id=claim.id)
    backend = FakeLLMBackend({ident: [plan(form, review).model_dump_json()]})
    outcome = asyncio.run(
        ResearchManager(StructuredCaller(backend)).plan(
            store.snapshot(project.id),
            task_id=ident,
            event="observations_recorded",
        )
    )
    assert outcome.task.status == "completed"
    context = json.loads(backend.requests[0].context_json)
    assert context["frontier"]["recent_observations"][0] == observation.model_dump(mode="json")
    new_claim = writer.apply(outcome.output.actions[0], cycle=2)
    assert store.claim_provenance(new_claim.id)[0].observation == observation
    assert outcome.output.actions[1] == review
    assert store.get(m.Observation, observation.id) == observation
    assert store.get(m.Run, observation.run_id).evaluation == observation.evaluation


def test_oversized_output_repairs_once_and_empty_ideas_are_valid(setup):
    _, _, _, dispatch, _ = setup
    task = dispatch.action.tasks[0]
    valid = json.loads(reply(task))
    valid["ideas"] = []
    backend = FakeLLMBackend({task.id: ["x" * 2000, json.dumps(valid)]})
    outcome = asyncio.run(
        ResearchAgent(StructuredCaller(backend, max_output_chars=1000)).explore(task)
    )
    assert outcome.task.status == "completed" and outcome.output.ideas == []
    assert outcome.task.repair_attempts == 1
    assert len(backend.requests[-1].previous_output) == 1000


def test_all_ra_failures_are_available_to_synthesis(setup):
    store, _, project, dispatch, _ = setup
    backend = FakeLLMBackend({task.id: [RuntimeError("offline")] for task in dispatch.action.tasks})
    batch = asyncio.run(
        ResearchDispatcher(ResearchAgent(StructuredCaller(backend))).dispatch(dispatch)
    )
    assert not batch.completed_results
    ident = uuid4()
    backend = FakeLLMBackend(
        {
            ident: [
                plan(
                    action(
                        project,
                        "pause_for_human",
                        question_for_human="Provider unavailable",
                        options=["retry", "stop"],
                    )
                ).model_dump_json()
            ]
        }
    )
    outcome = asyncio.run(
        ResearchManager(StructuredCaller(backend)).synthesize(
            store.snapshot(project.id),
            batch,
            task_id=ident,
        )
    )
    assert outcome.task.status == "completed"
    assert len(json.loads(backend.requests[0].context_json)["exploration"]["outcomes"]) == 3


def test_synthesis_rejects_duplicate_or_cross_project_results(setup):
    store, _, project, dispatch, _ = setup
    backend = FakeLLMBackend({task.id: [reply(task)] for task in dispatch.action.tasks})
    batch = asyncio.run(
        ResearchDispatcher(ResearchAgent(StructuredCaller(backend))).dispatch(dispatch)
    )
    manager = ResearchManager(StructuredCaller(FakeLLMBackend({})))
    batch.outcomes.append(batch.outcomes[0])
    with pytest.raises(ValueError, match="Duplicate"):
        asyncio.run(manager.synthesize(store.snapshot(project.id), batch, task_id=uuid4()))
    batch.outcomes.pop()
    batch.outcomes[0].task.project_id = uuid4()
    with pytest.raises(ValueError, match="another project"):
        asyncio.run(manager.synthesize(store.snapshot(project.id), batch, task_id=uuid4()))


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_synthesis_cannot_recursively_request_itself(setup, repair_succeeds):
    store, _, project, dispatch, _ = setup
    backend = FakeLLMBackend({task.id: [reply(task)] for task in dispatch.action.tasks})
    batch = asyncio.run(
        ResearchDispatcher(ResearchAgent(StructuredCaller(backend))).dispatch(dispatch)
    )
    ident = uuid4()
    bad = plan(
        action(project, "synthesize", task_ids=[task.id for task in dispatch.action.tasks])
    ).model_dump_json()
    good = plan(
        action(
            project,
            "create_hypothesis",
            subproblem_id=dispatch.action.tasks[0].subproblem_id,
            statement="The combined evidence motivates a controlled pilot.",
            rationale="Assess the joined results before choosing an experiment",
        )
    ).model_dump_json()
    backend = FakeLLMBackend({ident: [bad, good if repair_succeeds else bad]})
    outcome = asyncio.run(
        ResearchManager(StructuredCaller(backend)).synthesize(
            store.snapshot(project.id), batch, task_id=ident
        )
    )
    assert outcome.task.status == ("completed" if repair_succeeds else "failed")
    assert outcome.task.repair_attempts == 1
    assert len(backend.requests) == 2
    assert "already synthesis" in backend.requests[1].repair_error
    assert "This call IS the synthesis step" in backend.requests[0].system_prompt
    assert not store.list(m.Hypothesis)  # A role proposes; only host code applies it.


def test_research_state_writer_rejects_foreign_hypothesis_without_decision(setup):
    store, writer, project, _, _ = setup
    other_data = project.model_dump()
    other_data.update(id=uuid4(), main_question_approved=False)
    other = store.create(m.Project.model_validate(other_data))
    entities = json.loads((EXAMPLES / "entities.json").read_text())
    sub_data = entities["subproblem"]
    sub_data.update(id=str(uuid4()), project_id=str(other.id))
    sub = store.create(m.Subproblem.model_validate(sub_data))
    hyp_data = entities["hypothesis"]
    hyp_data.update(id=str(uuid4()), subproblem_id=str(sub.id), branch_id=None, status="proposed")
    hyp = store.create(m.Hypothesis.model_validate(hyp_data))
    before = store.list(m.Decision)
    with pytest.raises(StateError, match="Cross-project"):
        writer.apply(action(project, "select_hypothesis", hypothesis_id=hyp.id), cycle=1)
    assert store.list(m.Decision) == before
