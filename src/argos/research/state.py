"""Trusted host adapter, separate from agent reasoning and LLM requests.

This is not the full orchestrator: it applies research proposals only. Execution,
review dispatch, cycle recovery and the human CLI are integration responsibilities.
"""

from datetime import UTC, datetime
from uuid import uuid4

from argos import models as m
from argos.common import EntityId, EntityReference
from argos.protocols import (
    CloseSubproblem,
    CreateHypothesis,
    CreateSubproblem,
    DispatchResearchAgents,
    FormClaim,
    ManagerAction,
    ProposeExperiment,
    ProposeMainQuestionRevision,
    ReviseClaim,
    SelectHypothesis,
    UpdateSubproblem,
)
from argos.state import StateStore
from argos.state.store import KINDS, TYPES, StateError


class ResearchStateWriter:
    def __init__(self, store: StateStore):
        self.store = store

    def record_task(self, task: m.Task) -> None:
        """Hook for StructuredCaller; duplicate terminal IDs cannot be reused."""
        try:
            self.store.get(m.Task, task.id)
        except KeyError:
            self.store.create(task)
        else:
            self.store.update(task)

    def apply(self, action: ManagerAction, *, cycle: int) -> m.Entity | list[m.ResearchBranch]:
        """Apply one validated action and record its rationale atomically.

        Generated entities are returned so subsequent RM calls can use their stable IDs.
        Actions handled by execution/review/orchestrator code explicitly raise rather
        than silently pretending to have executed.
        """
        action = ManagerAction.model_validate_json(action.model_dump_json())
        payload = action.action
        now = datetime.now(UTC)
        with self.store.transaction():
            project = self.store.get(m.Project, action.project_id)
            if not project.main_question_approved:
                raise StateError("Human must first approve the main question")
            if project.status != "active":
                raise StateError("Research project is not active")

            def local(cls, ident):
                entity = self.store.get(cls, ident)
                # Query membership via the public store API, including indirect parents.
                if not any(e.id == ident for e in self.store.list(cls, project_id=project.id)):
                    raise StateError("Cross-project research action")
                return entity

            result: m.Entity | list[m.ResearchBranch]
            if isinstance(payload, CreateSubproblem):
                result = self.store.create(
                    m.Subproblem(
                        id=uuid4(),
                        created_at=now,
                        updated_at=now,
                        project_id=project.id,
                        question=payload.question,
                        priority=payload.priority,
                    )
                )
            elif isinstance(payload, (UpdateSubproblem, CloseSubproblem)):
                entity = local(m.Subproblem, payload.subproblem_id)
                if isinstance(payload, UpdateSubproblem):
                    entity.question = payload.question
                    if payload.priority is not None:
                        entity.priority = payload.priority
                else:
                    entity.status = "resolved"
                entity.updated_at = now
                result = self.store.update(entity)
            elif isinstance(payload, DispatchResearchAgents):
                branches = []
                for task in payload.tasks:
                    local(m.Subproblem, task.subproblem_id)
                    if task.context_policy != "independent":
                        raise StateError("Initial RA contexts must be independent")
                    if task.main_research_question != project.config.main_research_question:
                        raise StateError("Task question differs from canonical question")
                    try:
                        branch = local(m.ResearchBranch, task.branch_id)
                    except KeyError:
                        branch = self.store.create(
                            m.ResearchBranch(
                                id=task.branch_id,
                                created_at=now,
                                subproblem_id=task.subproblem_id,
                                name=task.perspective,
                                description=task.question,
                            )
                        )
                    if (
                        branch.subproblem_id != task.subproblem_id
                        or branch.context_policy != "independent"
                        or branch.status != "active"
                    ):
                        raise StateError(
                            "Task branch is not an active independent branch of subproblem"
                        )
                    # Validate evidence membership, without giving the RA other branches' context.
                    for evidence in task.evidence:
                        local(TYPES[evidence.entity_type], evidence.entity_id)
                    branches.append(branch)
                result = branches
            elif isinstance(payload, CreateHypothesis):
                local(m.Subproblem, payload.subproblem_id)
                if payload.branch_id:
                    local(m.ResearchBranch, payload.branch_id)
                result = self.store.create(
                    m.Hypothesis(
                        id=uuid4(),
                        created_at=now,
                        updated_at=now,
                        subproblem_id=payload.subproblem_id,
                        branch_id=payload.branch_id,
                        statement=payload.statement,
                        rationale=payload.rationale,
                    )
                )
            elif isinstance(payload, SelectHypothesis):
                hypothesis = local(m.Hypothesis, payload.hypothesis_id)
                if hypothesis.status not in ("proposed", "inconclusive", "testing"):
                    raise StateError(
                        "Selection requires a proposed, inconclusive or testing hypothesis"
                    )
                # Selection is a decision, not proof, and not yet a running experiment.
                result = hypothesis
            elif isinstance(payload, ProposeExperiment):
                hypothesis = local(m.Hypothesis, payload.spec.hypothesis_id)
                spec = payload.spec
                if spec.baseline_id != project.baseline_id or spec.scope != project.config.scope:
                    raise StateError("Experiment must preserve baseline and path scope")
                for key, ceiling in project.config.resource_limits.model_dump().items():
                    value = getattr(spec.resource_limits, key)
                    if ceiling is not None and (value is None or value > ceiling):
                        raise StateError("Experiment exceeds resource limits")
                result = self.store.create(
                    m.Experiment(
                        id=spec.experiment_id,
                        created_at=now,
                        spec=spec,
                        branch_id=hypothesis.branch_id,
                    )
                )
            elif isinstance(payload, FormClaim):
                result = self.store.create(
                    m.Claim(
                        id=uuid4(),
                        created_at=now,
                        project_id=project.id,
                        statement=payload.statement,
                        evidence=payload.evidence,
                    )
                )
            elif isinstance(payload, ReviseClaim):
                claim = local(m.Claim, payload.claim_id)
                claim.statement = payload.statement
                claim.evidence = payload.evidence
                result = self.store.update(claim)
            elif isinstance(payload, ProposeMainQuestionRevision):
                project.proposed_main_research_question = payload.proposed_question
                project.status = "paused"
                result = self.store.update(project)
            else:
                raise NotImplementedError(f"Host must handle action {payload.action_type}")
            self.store.create(
                m.Decision(
                    id=uuid4(),
                    created_at=now,
                    project_id=project.id,
                    cycle=cycle,
                    decision_type=payload.action_type,
                    actor="rm",
                    summary=payload.action_type,
                    rationale=action.rationale,
                    references=[
                        EntityReference(entity_type=KINDS[type(entity)], entity_id=entity.id)
                        for entity in (result if isinstance(result, list) else [result])
                    ],
                    action=action,
                )
            )
            return result

    def answer_main_question(
        self,
        project_id: EntityId,
        *,
        proposed_question: str,
        approve: bool,
        rationale: str,
        cycle: int,
    ) -> m.Project:
        """Trusted human-interface ONLY. Never route an LLM response here.

        Exact proposal matching prevents a stale approval from accepting new wording.
        Authentication / interactive input belongs to the host, not an agent flag.
        """
        if type(approve) is not bool:
            raise ValueError("approve must be an explicit boolean")
        with self.store.transaction():
            project = self.store.get(m.Project, project_id)
            if (
                project.status != "paused"
                or project.proposed_main_research_question != proposed_question
            ):
                raise StateError("No matching pending main-question gate")
            if approve:
                project = self.store.approve_main_question(
                    project_id,
                    proposed_question,
                    rationale=rationale,
                    cycle=cycle,
                )
            else:
                self.store.create(
                    m.Decision(
                        id=uuid4(),
                        created_at=datetime.now(UTC),
                        project_id=project_id,
                        cycle=cycle,
                        decision_type="reject_main_question_revision",
                        actor="human",
                        summary="Human rejected the proposed main question",
                        rationale=rationale,
                        references=[EntityReference(entity_type="project", entity_id=project_id)],
                    )
                )
                project.proposed_main_research_question = None
            project.status = "active"
            return self.store.update(project)
