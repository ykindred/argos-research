"""Bounded structured calls; this infrastructure never applies scientific state."""

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, model_validator

from argos.backends import LLMBackend, LLMRequest
from argos.common import EntityId, EntityReference, Model
from argos.models import Task

T = TypeVar("T", bound=BaseModel)


class AgentOutcome(Model, Generic[T]):
    task: Task
    output: T | None = None

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.task.status not in ("completed", "failed", "timeout", "cancelled"):
            raise ValueError("Agent outcome requires a terminal task")
        if (self.task.status == "completed") != (self.output is not None):
            raise ValueError("Only completed tasks publish validated output")
        if self.output is not None and hasattr(self.output, "task_id"):
            if self.output.task_id != self.task.id:
                raise ValueError("Output task identity must match its task record")
        return self


class TaskRecordingError(RuntimeError):
    """The host could not durably record a task transition."""


class StructuredCaller:
    """Share one caller across roles for a finite LLM semaphore.

    `record_task` is a trusted host callback (e.g. create/update StateStore Task).
    It runs before calls, before repair, and at completion. Persistence errors propagate;
    backend/validation failures become failed Tasks. No database is exposed to the LLM.
    """

    def __init__(
        self,
        backend: LLMBackend,
        *,
        llm_slots: int = 4,
        timeout_seconds: float = 60,
        max_context_chars: int = 60_000,
        max_output_chars: int = 60_000,
        record_task: Callable[[Task], None] | None = None,
        record_outcome: Callable[[AgentOutcome], None] | None = None,
    ):
        if type(llm_slots) is not int or llm_slots < 1:
            raise ValueError("llm_slots must be a positive integer")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        for value in (max_context_chars, max_output_chars):
            if type(value) is not int or value < 1:
                raise ValueError("character limits must be positive integers")
        self.backend = backend
        self.semaphore = asyncio.Semaphore(llm_slots)
        self.timeout_seconds = timeout_seconds
        self.max_context_chars = max_context_chars
        self.max_output_chars = max_output_chars
        self.record_task = record_task
        self.record_outcome = record_outcome

    def _record(self, task: Task):
        if self.record_task:
            try:
                self.record_task(task.model_copy(deep=True))
            except Exception as exc:
                raise TaskRecordingError("Task persistence failed") from exc

    async def call(
        self,
        request: LLMRequest,
        project_id: EntityId,
        output_type: type[T],
        *,
        references: list[EntityReference] | None = None,
        validate: Callable[[T], None] | None = None,
    ) -> AgentOutcome[T]:
        now = datetime.now(UTC)
        task = Task(
            id=request.task_id,
            project_id=project_id,
            created_at=now,
            kind=request.role,
            resource_class="llm",
            references=references or [],
            status="running",
            started_at=now,
        )
        self._record(task)
        output = None
        failure = None
        status = "failed"
        try:
            if len(request.context_json) > self.max_context_chars:
                raise ValueError("Context exceeds character budget; provide a smaller briefing")
            # The deadline includes queue time and both attempts, not a fresh timeout per repair.
            async with asyncio.timeout(self.timeout_seconds):
                async with self.semaphore:
                    for attempt in range(2):
                        raw = await self.backend.complete(request.model_copy(deep=True))
                        try:
                            if not isinstance(raw, str) or len(raw) > self.max_output_chars:
                                raise ValueError("Backend output is not bounded JSON text")
                            candidate = output_type.model_validate_json(raw)
                            if validate:
                                validate(candidate)
                            output = candidate
                            status = "completed"
                            break
                        except ValueError as exc:
                            if attempt:
                                raise ValueError(
                                    f"Invalid structured output after one repair: {exc}"
                                )
                            task.repair_attempts = 1
                            self._record(task)
                            request = request.model_copy(
                                update={
                                    "repair_error": str(exc)[:2000],
                                    "previous_output": raw[: self.max_output_chars]
                                    if isinstance(raw, str)
                                    else None,
                                },
                                deep=True,
                            )
        except TimeoutError:
            status, failure = "timeout", "LLM task deadline exceeded"
        except asyncio.CancelledError:
            self._record(
                Task.model_validate(
                    {
                        **task.model_dump(),
                        "status": "cancelled",
                        "finished_at": datetime.now(UTC),
                    }
                )
            )
            raise
        except TaskRecordingError:
            raise
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"[:4000]
        finished = Task.model_validate(
            {
                **task.model_dump(),
                "status": status,
                "failure": failure,
                "finished_at": datetime.now(UTC),
            }
        )
        outcome = AgentOutcome[output_type](task=finished, output=output)
        if self.record_outcome:
            try:
                self.record_outcome(outcome)
            except Exception as exc:
                raise TaskRecordingError("Outcome persistence failed") from exc
        else:
            self._record(finished)
        return outcome
