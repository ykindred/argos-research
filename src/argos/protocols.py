"""Validated messages between components; no execution or state mutation logic."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from argos.common import (
    CommandRecord,
    ConstraintCheck,
    EntityId,
    EntityReference,
    EvaluationProtocol,
    ExecutionFailure,
    Measurement,
    Model,
    PathScope,
    ResourceLimits,
    RunStatus,
    Text,
    Timestamp,
)


class ProjectConfig(Model):
    research_context: Text
    main_research_question: Text
    source_repository: Text
    build_command: list[Text] = Field(min_length=1)
    test_command: list[Text] = Field(min_length=1)
    evaluation_protocol: EvaluationProtocol
    scope: PathScope
    resource_limits: ResourceLimits


class ResearchTask(Model):
    id: EntityId
    project_id: EntityId
    subproblem_id: EntityId
    question: Text
    main_research_question: Text
    context: Text
    perspective: Text
    evidence: list[EntityReference]


class ResearchIdea(Model):
    idea: Text
    hypothesis: Text
    rationale: Text
    expected_effects: list[Text] = Field(min_length=1)
    validation_methods: list[Text] = Field(min_length=1)
    risks: list[Text]
    assumptions: list[Text]


class ResearchAgentResult(Model):
    task_id: EntityId
    agent_id: Text
    created_at: Timestamp
    ideas: list[ResearchIdea]
    summary: Text


class ExperimentSpec(Model):
    experiment_id: EntityId
    hypothesis_id: EntityId
    hypothesis_statement: Text
    goal: Text
    requested_change: Text
    build_steps: list[list[Text]]
    test_steps: list[list[Text]]
    run_steps: list[list[Text]] = Field(min_length=1)
    evaluation_protocol: EvaluationProtocol
    resource_limits: ResourceLimits
    scope: PathScope | None = None

    @model_validator(mode="after")
    def nonempty_commands(self) -> Self:
        if any(not step for step in self.build_steps + self.test_steps + self.run_steps):
            raise ValueError("Execution steps must contain a nonempty argv")
        return self


class ExperimentResult(Model):
    experiment_id: EntityId
    run_id: EntityId
    status: Literal[RunStatus.SUCCEEDED, RunStatus.FAILED]
    started_at: Timestamp
    finished_at: Timestamp
    source_commit: Text
    resulting_commit: Text | None
    configuration: dict[str, JsonValue]
    commands: list[CommandRecord]
    artifacts: list[Text]
    failure: ExecutionFailure | None = None

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if (self.status == RunStatus.FAILED) != (self.failure is not None):
            raise ValueError("Exactly failed results must include an execution failure")
        if self.status == RunStatus.SUCCEEDED:
            if self.resulting_commit is None or not self.commands:
                raise ValueError("Successful execution requires a resulting commit and commands")
            if any(command.exit_code != 0 for command in self.commands):
                raise ValueError("Successful execution requires all commands to exit with zero")
        return self


class EvaluatorResult(Model):
    experiment_id: EntityId
    run_id: EntityId
    evaluated_at: Timestamp
    protocol_name: Text
    measurements: list[Measurement] = Field(min_length=1)
    constraint_checks: list[ConstraintCheck]
    artifacts: list[Text]

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        for items in (self.measurements, self.constraint_checks):
            if len({item.name for item in items}) != len(items):
                raise ValueError("Measurement and constraint names must be unique within each list")
        return self


class CriticVerdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_MORE_EVIDENCE = "needs_more_evidence"


class CriticReview(Model):
    claim_id: EntityId
    created_at: Timestamp
    verdict: CriticVerdict
    rationale: Text
    weaknesses: list[Text]
    risks: list[Text]
    requested_checks: list[Text]


class ManagerActionType(StrEnum):
    CREATE_SUBPROBLEM = "create_subproblem"
    UPDATE_SUBPROBLEM = "update_subproblem"
    DISPATCH_RESEARCH_AGENTS = "dispatch_research_agents"
    CREATE_HYPOTHESIS = "create_hypothesis"
    SELECT_HYPOTHESIS = "select_hypothesis"
    PROPOSE_EXPERIMENT = "propose_experiment"
    FORM_CLAIM = "form_claim"
    REVISE_CLAIM = "revise_claim"
    REQUEST_CRITIC_REVIEW = "request_critic_review"
    CONTINUE_RESEARCH = "continue_research"
    PAUSE_FOR_HUMAN = "pause_for_human"
    PROPOSE_MAIN_QUESTION_REVISION = "propose_main_question_revision"


class CreateSubproblem(Model):
    action_type: Literal[ManagerActionType.CREATE_SUBPROBLEM]
    question: Text


class UpdateSubproblem(Model):
    action_type: Literal[ManagerActionType.UPDATE_SUBPROBLEM]
    subproblem_id: EntityId
    question: Text


class DispatchResearchAgents(Model):
    action_type: Literal[ManagerActionType.DISPATCH_RESEARCH_AGENTS]
    tasks: list[ResearchTask] = Field(min_length=1)


class CreateHypothesis(Model):
    action_type: Literal[ManagerActionType.CREATE_HYPOTHESIS]
    subproblem_id: EntityId
    statement: Text
    rationale: Text
    branch_id: EntityId | None = None


class SelectHypothesis(Model):
    action_type: Literal[ManagerActionType.SELECT_HYPOTHESIS]
    hypothesis_id: EntityId


class ProposeExperiment(Model):
    action_type: Literal[ManagerActionType.PROPOSE_EXPERIMENT]
    spec: ExperimentSpec


class ClaimEvidence(Model):
    supporting_observation_ids: list[EntityId]
    contradicting_observation_ids: list[EntityId]

    @model_validator(mode="after")
    def distinct_evidence(self) -> Self:
        support = set(self.supporting_observation_ids)
        contradict = set(self.contradicting_observation_ids)
        if not support and not contradict:
            raise ValueError("Claims must reference at least one observation")
        if support & contradict:
            raise ValueError("An observation cannot both support and contradict the same claim")
        if len(support) != len(self.supporting_observation_ids) or len(contradict) != len(
            self.contradicting_observation_ids
        ):
            raise ValueError("Duplicate observation references are not allowed")
        return self


class FormClaim(Model):
    action_type: Literal[ManagerActionType.FORM_CLAIM]
    statement: Text
    evidence: ClaimEvidence


class ReviseClaim(Model):
    action_type: Literal[ManagerActionType.REVISE_CLAIM]
    claim_id: EntityId
    statement: Text
    evidence: ClaimEvidence


class RequestCriticReview(Model):
    action_type: Literal[ManagerActionType.REQUEST_CRITIC_REVIEW]
    claim_id: EntityId


class ContinueResearch(Model):
    action_type: Literal[ManagerActionType.CONTINUE_RESEARCH]
    next_question: Text


class PauseForHuman(Model):
    action_type: Literal[ManagerActionType.PAUSE_FOR_HUMAN]
    question_for_human: Text


class ProposeMainQuestionRevision(Model):
    action_type: Literal[ManagerActionType.PROPOSE_MAIN_QUESTION_REVISION]
    proposed_question: Text
    requires_human_approval: Literal[True] = True


ActionPayload = Annotated[
    CreateSubproblem
    | UpdateSubproblem
    | DispatchResearchAgents
    | CreateHypothesis
    | SelectHypothesis
    | ProposeExperiment
    | FormClaim
    | ReviseClaim
    | RequestCriticReview
    | ContinueResearch
    | PauseForHuman
    | ProposeMainQuestionRevision,
    Field(discriminator="action_type"),
]


class ManagerAction(Model):
    project_id: EntityId
    rationale: Text
    action: ActionPayload
