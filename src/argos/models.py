"""Persistent research entities. References are stable UUIDs, not embedded state."""

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from argos.common import (
    Confidence,
    ContextPolicy,
    EntityId,
    EntityReference,
    EntityStatus,
    ExperimentStatus,
    HypothesisStatus,
    Model,
    ObservationRelation,
    ResourceClass,
    RunStatus,
    SubproblemStatus,
    TaskStatus,
    Text,
    Timestamp,
)
from argos.protocols import (
    ClaimEvidence,
    EvaluatorResult,
    ExperimentResult,
    ExperimentSpec,
    ManagerAction,
    ProjectConfig,
)


class Entity(Model):
    id: EntityId
    created_at: Timestamp


class Project(Entity):
    name: Text
    config: ProjectConfig
    baseline_id: EntityId | None = None
    status: EntityStatus = EntityStatus.ACTIVE


class Subproblem(Entity):
    project_id: EntityId
    question: Text
    parent_id: EntityId | None = None
    priority: Confidence = 0.5
    updated_at: Timestamp
    status: SubproblemStatus = SubproblemStatus.OPEN


class ResearchBranch(Entity):
    subproblem_id: EntityId
    name: Text
    description: Text
    context_policy: ContextPolicy = ContextPolicy.INDEPENDENT
    status: EntityStatus = EntityStatus.ACTIVE


class Hypothesis(Entity):
    subproblem_id: EntityId
    branch_id: EntityId | None = None
    statement: Text
    rationale: Text
    confidence: Confidence | None = None
    updated_at: Timestamp
    status: HypothesisStatus = HypothesisStatus.PROPOSED


class Experiment(Entity):
    spec: ExperimentSpec
    branch_id: EntityId | None = None
    status: ExperimentStatus = ExperimentStatus.PLANNED

    @model_validator(mode="after")
    def matching_spec(self) -> Self:
        if self.spec.experiment_id != self.id:
            raise ValueError("Experiment ID must match its spec")
        return self


class Run(Entity):
    experiment_id: EntityId
    status: RunStatus = RunStatus.PENDING
    result: ExperimentResult | None = None
    evaluation: EvaluatorResult | None = None

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        terminal = self.status in (RunStatus.SUCCEEDED, RunStatus.FAILED)
        if terminal != (self.result is not None):
            raise ValueError("Exactly terminal runs must contain a result")
        for record in (self.result, self.evaluation):
            if record and (record.run_id != self.id or record.experiment_id != self.experiment_id):
                raise ValueError("Run and experiment references must match nested records")
        if self.result and self.result.status != self.status:
            raise ValueError("Run status must match the result")
        if self.evaluation and self.status != RunStatus.SUCCEEDED:
            raise ValueError("Measurements require successful execution")
        return self


class Observation(Entity):
    run_id: EntityId
    evaluation: EvaluatorResult
    summary: Text
    relation: ObservationRelation = ObservationRelation.NEUTRAL
    confidence: Confidence | None = None

    @model_validator(mode="after")
    def matching_run(self) -> Self:
        if self.evaluation.status == "invalid" and self.relation != "invalid":
            raise ValueError("Invalid evaluation cannot support scientific interpretation")
        if self.evaluation.run_id != self.run_id:
            raise ValueError("Observation run must match its evaluation")
        return self


class Claim(Entity):
    project_id: EntityId
    statement: Text
    evidence: ClaimEvidence
    status: EntityStatus = EntityStatus.ACTIVE


class DecisionActor(StrEnum):
    RM = "rm"
    HUMAN = "human"


class Decision(Entity):
    project_id: EntityId
    cycle: int = Field(ge=0, strict=True)
    decision_type: Text
    actor: DecisionActor
    summary: Text
    rationale: Text
    references: list[EntityReference]
    action: ManagerAction | None = None

    @model_validator(mode="after")
    def matching_project(self) -> Self:
        if self.action and self.action.project_id != self.project_id:
            raise ValueError("Decision and action must refer to the same project")
        return self


class Task(Entity):
    """Durable work item, distinct from the bounded RA ResearchTask message."""

    project_id: EntityId
    kind: Text
    resource_class: ResourceClass
    references: list[EntityReference]
    status: TaskStatus = TaskStatus.PENDING
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None
    failure: Text | None = None
    output_references: list[EntityReference] = Field(default_factory=list)
    repair_attempts: int = Field(default=0, ge=0, le=1, strict=True)

    @model_validator(mode="after")
    def consistent_lifecycle(self) -> Self:
        terminal = self.status in ("completed", "failed", "timeout", "cancelled")
        if terminal != (self.finished_at is not None):
            raise ValueError("Exactly terminal tasks require finished_at")
        if self.status == "running" and self.started_at is None:
            raise ValueError("Running tasks require started_at")
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("Task finish must not precede start")
        if (self.status in ("failed", "timeout")) != (self.failure is not None):
            raise ValueError("Exactly failed or timed-out tasks require failure details")
        if self.status != "completed" and self.output_references:
            raise ValueError("Only completed tasks may publish validated outputs")
        return self


class Evidence(Entity):
    claim_id: EntityId
    observation_id: EntityId
    run_id: EntityId
    relation: ObservationRelation


class Baseline(Entity):
    """Append-only snapshot; authorization and clean-checkout checks belong to runtime."""

    project_id: EntityId
    run_id: EntityId
    source_commit: Text
    evaluation: EvaluatorResult
    previous_baseline_id: EntityId | None = None
    approval_decision_id: EntityId

    @model_validator(mode="after")
    def valid_baseline(self) -> Self:
        if self.evaluation.run_id != self.run_id or self.evaluation.status != "ok":
            raise ValueError("Baseline requires a matching valid evaluation")
        if self.previous_baseline_id == self.id:
            raise ValueError("Baseline cannot replace itself")
        return self
