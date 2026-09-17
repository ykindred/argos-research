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


class ExperimentStatus(StrEnum):
    PROPOSED = "proposed"
    SELECTED = "selected"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


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


class EntityReference(Model):
    entity_type: EntityType
    entity_id: EntityId


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


class Measurement(Model):
    name: Text
    value: FiniteNumber
    unit: Text | None = None


class ConstraintCheck(Model):
    name: Text
    passed: bool = Field(strict=True)
    measured_value: FiniteNumber
    threshold: FiniteNumber


class ExecutionFailureType(StrEnum):
    BUILD_FAILURE = "build_failure"
    TEST_FAILURE = "test_failure"
    RUNTIME_CRASH = "runtime_crash"
    TIMEOUT = "timeout"
    INVALID_COMMAND = "invalid_command"
    INVALID_EVALUATOR_OUTPUT = "invalid_evaluator_output"


class ExecutionFailure(Model):
    kind: ExecutionFailureType
    message: Text


class CommandRecord(Model):
    argv: list[Text] = Field(min_length=1)
    exit_code: int | None
    stdout: str
    stderr: str
