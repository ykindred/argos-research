"""Validated messages between components; no execution or state mutation logic."""

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from argos.common import (
    CommandRecord,
    Confidence,
    ConstraintCheck,
    ContextPolicy,
    CycleLimits,
    EntityId,
    EntityReference,
    EvaluationProtocol,
    ExecutionFailure,
    FiniteNumber,
    Measurement,
    MetricDirection,
    Model,
    PathScope,
    ResourceClass,
    ResourceLimits,
    ResourceSlots,
    RunStatus,
    Text,
    Timestamp,
)


class ProjectConfig(Model):
    research_charter: Text
    research_direction: Text
    held_out_test_protocol: Text
    resources: ResourceSlots = Field(default_factory=ResourceSlots)
    limits: CycleLimits = Field(default_factory=CycleLimits)
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
    branch_id: EntityId
    context_policy: ContextPolicy = ContextPolicy.INDEPENDENT
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
    falsification_suggestions: list[Text] = Field(default_factory=list)


class ResearchAgentResult(Model):
    task_id: EntityId
    agent_id: Text
    created_at: Timestamp
    ideas: list[ResearchIdea]
    summary: Text


class ArtifactRequirement(Model):
    """Producer is explicit; host paths refer to the trusted evidence directory."""

    path: Text
    producer: Literal["host", "coding", "experiment"]

    @model_validator(mode="after")
    def safe_path(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            not path.parts
            or path.is_absolute()
            or ".." in path.parts
            or ".git" in path.parts
            or "\\" in self.path
        ):
            raise ValueError("Artifact must have a safe relative path")
        if self.producer == "host" and self.path not in {
            "code.diff",
            "stdout.log",
            "stderr.log",
            "revision.json",
        }:
            raise ValueError("Host artifacts are code.diff, stdout.log, stderr.log, revision.json")
        return self


class ExperimentSpec(Model):
    experiment_id: EntityId
    hypothesis_id: EntityId
    hypothesis_statement: Text
    goal: Text
    prediction: Text
    success_criteria: list[Text] = Field(min_length=1)
    failure_criteria: list[Text] = Field(min_length=1)
    resource_class: ResourceClass = ResourceClass.CPU
    baseline_id: EntityId | None = None
    requested_change: Text
    build_steps: list[list[Text]]
    test_steps: list[list[Text]]
    run_steps: list[list[Text]] = Field(min_length=1)
    evaluation_protocol: EvaluationProtocol
    resource_limits: ResourceLimits
    scope: PathScope | None = None
    required_artifacts: list[ArtifactRequirement | Text] = Field(default_factory=list)
    retry_of: EntityId | None = None
    recovery_rationale: Text | None = None

    @property
    def artifact_requirements(self) -> list[ArtifactRequirement]:
        return [
            ArtifactRequirement(path=a, producer="experiment") if isinstance(a, str) else a
            for a in self.required_artifacts
        ]

    @model_validator(mode="after")
    def nonempty_commands(self) -> Self:
        if (self.retry_of is None) != (self.recovery_rationale is None):
            raise ValueError("Retry requires both retry_of and a concrete recovery_rationale")
        if self.retry_of == self.experiment_id:
            raise ValueError("Retry must create a new experiment identity")
        keys = [
            ("experiment", a) if isinstance(a, str) else (a.producer, a.path)
            for a in self.required_artifacts
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate artifact requirement")
        if any(not step for step in self.build_steps + self.test_steps + self.run_steps):
            raise ValueError("Execution steps must contain a nonempty argv")
        return self


class ExperimentResult(Model):
    experiment_id: EntityId
    run_id: EntityId
    status: Literal[RunStatus.SUCCEEDED, RunStatus.FAILED]
    started_at: Timestamp
    finished_at: Timestamp
    worktree: Text
    diff_path: Text
    stdout_path: Text
    stderr_path: Text
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


class MetricComparison(Model):
    name: Text
    baseline_value: FiniteNumber
    value: FiniteNumber
    delta: FiniteNumber
    percent_change: FiniteNumber | None = None
    unit: Text | None = None
    direction: MetricDirection


class EvaluationProvenance(Model):
    protocol: EvaluationProtocol
    parser: Text
    input_path: Text
    stdout_path: Text
    stderr_path: Text
    worktree: Text
    source_commit: Text
    resulting_commit: Text
    command: CommandRecord | None = None


class EvaluatorResult(Model):
    experiment_id: EntityId
    run_id: EntityId
    evaluated_at: Timestamp
    status: Literal["ok", "invalid"] = "ok"
    baseline_id: EntityId | None = None
    protocol_name: Text
    measurements: list[Measurement]
    constraint_checks: list[ConstraintCheck]
    artifacts: list[Text]
    comparisons: list[MetricComparison] = Field(default_factory=list)
    provenance: EvaluationProvenance | None = None
    failure: ExecutionFailure | None = None

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        if self.status == "ok" and self.failure is not None:
            raise ValueError("Valid evaluation cannot include an execution failure")
        if self.comparisons and (self.baseline_id is None or self.status != "ok"):
            raise ValueError("Comparisons require a valid evaluation and pinned baseline")
        if self.status == "ok" and not self.measurements:
            raise ValueError("Valid evaluation requires measurements")
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
    measurement_comparability: Literal[
        "comparable", "not_comparable", "uncertain", "not_assessed"
    ] = "not_assessed"
    main_question_support: Literal["supports", "does_not_support", "uncertain", "not_assessed"] = (
        "not_assessed"
    )
    assessment_rationale: Text | None = None

    @model_validator(mode="after")
    def actionable_evidence_request(self) -> Self:
        if self.verdict == CriticVerdict.NEEDS_MORE_EVIDENCE and not self.requested_checks:
            raise ValueError("needs_more_evidence requires at least one specific requested check")
        return self


class ManagerActionType(StrEnum):
    CLOSE_SUBPROBLEM = "close_subproblem"
    IMPLEMENT_EXPERIMENT = "implement_experiment"
    RUN_EXPERIMENT = "run_experiment"
    SYNTHESIZE = "synthesize"
    STOP = "stop"
    PROPOSE_PROTECTED_CHANGE = "propose_protected_change"
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


class CloseSubproblem(Model):
    action_type: Literal[ManagerActionType.CLOSE_SUBPROBLEM]
    subproblem_id: EntityId


class ImplementExperiment(Model):
    action_type: Literal[ManagerActionType.IMPLEMENT_EXPERIMENT]
    experiment_id: EntityId


class RunExperiment(Model):
    action_type: Literal[ManagerActionType.RUN_EXPERIMENT]
    experiment_id: EntityId


class Synthesize(Model):
    action_type: Literal[ManagerActionType.SYNTHESIZE]
    task_ids: list[EntityId] = Field(min_length=1)


class Stop(Model):
    action_type: Literal[ManagerActionType.STOP]
    reason: Text


class ProtectedTarget(StrEnum):
    RESEARCH_DIRECTION = "research_direction"
    MAIN_QUESTION = "main_research_question"
    EVALUATOR = "evaluator"
    BASELINE = "baseline"
    HELD_OUT_TEST_PROTOCOL = "held_out_test_protocol"


class ProposeProtectedChange(Model):
    action_type: Literal[ManagerActionType.PROPOSE_PROTECTED_CHANGE]
    target: ProtectedTarget
    proposed_change: Text
    requires_human_approval: Literal[True] = True


class CreateSubproblem(Model):
    action_type: Literal[ManagerActionType.CREATE_SUBPROBLEM]
    question: Text
    priority: Confidence = 0.5


class UpdateSubproblem(Model):
    action_type: Literal[ManagerActionType.UPDATE_SUBPROBLEM]
    subproblem_id: EntityId
    question: Text
    priority: Confidence | None = None


class DispatchResearchAgents(Model):
    action_type: Literal[ManagerActionType.DISPATCH_RESEARCH_AGENTS]
    tasks: list[ResearchTask] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_tasks(self) -> Self:
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("Dispatch task IDs must be distinct")
        independent = [t.branch_id for t in self.tasks if t.context_policy == "independent"]
        if len(set(independent)) != len(independent):
            raise ValueError("Independent tasks must use distinct research branches")
        return self


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
    options: list[Text] = Field(default_factory=list)


class ProposeMainQuestionRevision(Model):
    action_type: Literal[ManagerActionType.PROPOSE_MAIN_QUESTION_REVISION]
    proposed_question: Text
    requires_human_approval: Literal[True] = True


ActionPayload = Annotated[
    CloseSubproblem
    | ImplementExperiment
    | RunExperiment
    | Synthesize
    | Stop
    | ProposeProtectedChange
    | CreateSubproblem
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

    @model_validator(mode="after")
    def matching_dispatch_project(self) -> Self:
        if isinstance(self.action, DispatchResearchAgents):
            if any(t.project_id != self.project_id for t in self.action.tasks):
                raise ValueError("Dispatched tasks must belong to the action project")
        return self


class CodingTask(Model):
    """Implementation-only request; later execution belongs to deterministic code."""

    task_id: EntityId
    experiment_id: EntityId
    requested_change: Text
    scope: PathScope
    project_scope: PathScope | None = None
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list)


class CodingResult(Model):
    task_id: EntityId
    experiment_id: EntityId
    status: Literal["implemented", "failed", "timeout"]
    diff_path: Text
    log_paths: list[Text] = Field(min_length=1)
    failure: ExecutionFailure | None = None

    @model_validator(mode="after")
    def consistent_failure(self) -> Self:
        if (self.status != "implemented") != (self.failure is not None):
            raise ValueError("Failed or timed-out coding must record failure")
        if self.failure and ((self.status == "timeout") != (self.failure.kind == "timeout")):
            raise ValueError("Coding timeout status and failure kind must match")
        return self
