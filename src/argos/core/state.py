"""Validated restart cursor; scientific records remain in the shared StateStore."""

from typing import Literal
from uuid import UUID

from pydantic import Field

from argos.common import Model, Text, Timestamp
from argos.protocols import ManagerAction
from argos.research.roles import ResearchBatch


class Operation(Model):
    kind: Literal["manager", "research", "execute", "evaluate", "critic"]
    id: UUID
    started_at: Timestamp
    action: ManagerAction | None = None
    run_id: UUID | None = None


class HumanGate(Model):
    id: UUID
    question: Text
    proposed_question: str | None = None
    protected_target: str | None = None
    options: list[str] = Field(default_factory=list)


class RuntimeState(Model):
    cycle: int = Field(default=0, ge=0, strict=True)
    experiments_started: int = Field(default=0, ge=0, strict=True)
    stagnant_cycles: int = Field(default=0, ge=0, strict=True)
    progress: list[int] = Field(default_factory=lambda: [0, 0, 0])
    actions: list[ManagerAction] = Field(default_factory=list)
    event: str = "initialized"
    batch: ResearchBatch | None = None
    evaluation_run: UUID | None = None
    operation: Operation | None = None
    gate: HumanGate | None = None
