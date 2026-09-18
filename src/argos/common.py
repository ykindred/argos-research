"""Domain-neutral validation primitives shared by state and messages."""

from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

EntityId = UUID
Timestamp = AwareDatetime
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
FiniteNumber = Annotated[float, Field(allow_inf_nan=False)]


class Model(BaseModel):
    """Reject unexpected agent fields; validate again at the persistence boundary."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EntityStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class ContextPolicy(StrEnum):
    INDEPENDENT = "independent"
    SHARED = "shared"
    BLIND = "blind"


class SubproblemStatus(StrEnum):
    OPEN = "open"
    ACTIVE = "active"
    BLOCKED = "blocked"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    TESTING = "testing"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"
    REJECTED = "rejected"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ResourceClass(StrEnum):
    LLM = "llm"
    CODING = "coding"
    CPU = "cpu"
    GPU = "gpu"


class ObservationRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"
    INVALID = "invalid"


class ExperimentStatus(StrEnum):
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    IMPLEMENTED = "implemented"
    TESTING = "testing"
    EVALUATING = "evaluating"
    IMPLEMENTATION_FAILED = "implementation_failed"
    TEST_FAILED = "test_failed"
    RUN_FAILED = "run_failed"
    INVALID_RESULT = "invalid_result"
    TIMEOUT = "timeout"
    PROPOSED = "proposed"
    SELECTED = "selected"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


RETRYABLE_EXPERIMENT_STATUSES = frozenset(
    {
        ExperimentStatus.IMPLEMENTATION_FAILED,
        ExperimentStatus.TEST_FAILED,
        ExperimentStatus.RUN_FAILED,
        ExperimentStatus.INVALID_RESULT,
        ExperimentStatus.TIMEOUT,
        ExperimentStatus.CANCELLED,
    }
)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EntityType(StrEnum):
    PROJECT = "project"
    SUBPROBLEM = "subproblem"
    RESEARCH_BRANCH = "research_branch"
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"
    RUN = "run"
    OBSERVATION = "observation"
    CLAIM = "claim"
    DECISION = "decision"
    TASK = "task"
    EVIDENCE = "evidence"
    BASELINE = "baseline"


class EntityReference(Model):
    entity_type: EntityType
    entity_id: EntityId


PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class ResourceSlots(Model):
    llm_slots: PositiveInt = 4
    coding_slots: PositiveInt = 1
    cpu_jobs: PositiveInt = 1
    gpu_jobs: PositiveInt = 1


class CycleLimits(Model):
    max_cycles: PositiveInt = 30
    max_experiments: PositiveInt = 50
    stagnation_cycles: PositiveInt = 5


class ResourceLimits(Model):
    timeout_seconds: Annotated[int, Field(strict=True, gt=0)]
    max_memory_mb: Annotated[int, Field(strict=True, gt=0)] | None = None
    max_cpu_cores: Annotated[int, Field(strict=True, gt=0)] | None = None


class PathScope(Model):
    """Repository-relative paths/globs; execution must enforce their meaning."""

    editable_paths: list[Text]
    protected_paths: list[Text]


class EvaluationProtocol(Model):
    name: Text
    command: list[Text] = Field(min_length=1)
    metric_names: list[Text] = Field(min_length=1)
    constraints: list[Text]


class MetricDirection(StrEnum):
    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    INFORMATIONAL = "informational"


class Measurement(Model):
    name: Text
    value: FiniteNumber
    direction: MetricDirection
    unit: Text | None = None


class ConstraintCheck(Model):
    name: Text
    passed: bool = Field(strict=True)
    measured_value: FiniteNumber | None = None
    threshold: FiniteNumber | None = None


class ExecutionFailureType(StrEnum):
    IMPLEMENTATION_FAILURE = "implementation_failure"
    BUILD_FAILURE = "build_failure"
    TEST_FAILURE = "test_failure"
    RUNTIME_CRASH = "runtime_crash"
    TIMEOUT = "timeout"
    INVALID_COMMAND = "invalid_command"
    INVALID_MODIFICATION = "invalid_modification"
    MISSING_ARTIFACT = "missing_artifact"
    INVALID_EVALUATOR_OUTPUT = "invalid_evaluator_output"


class ExecutionFailure(Model):
    kind: ExecutionFailureType
    message: Text


class CommandRecord(Model):
    argv: list[Text] = Field(min_length=1)
    exit_code: int | None
    stdout: str
    stderr: str
