"""Persistent research entities. References are stable UUIDs, not embedded state."""

from enum import StrEnum
from typing import Self

from pydantic import model_validator

from argos.common import (
    EntityId,
    EntityReference,
    EntityStatus,
    ExperimentStatus,
    Model,
    RunStatus,
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
    status: EntityStatus = EntityStatus.ACTIVE


class Subproblem(Entity):
    project_id: EntityId
    question: Text
    status: EntityStatus = EntityStatus.ACTIVE


class ResearchBranch(Entity):
    subproblem_id: EntityId
    name: Text
    description: Text
    status: EntityStatus = EntityStatus.ACTIVE


class Hypothesis(Entity):
    subproblem_id: EntityId
    branch_id: EntityId | None = None
    statement: Text
    rationale: Text
    status: EntityStatus = EntityStatus.ACTIVE


class Experiment(Entity):
    spec: ExperimentSpec
    branch_id: EntityId | None = None
    status: ExperimentStatus = ExperimentStatus.PROPOSED

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

    @model_validator(mode="after")
    def matching_run(self) -> Self:
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
