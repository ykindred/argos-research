"""Provider-neutral, stateless LLM requests and a scripted deterministic backend."""

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from argos.common import EntityId, Model, Text


class LLMRequest(Model):
    task_id: EntityId
    role: Literal["manager", "research_agent"]
    system_prompt: Text
    context_json: Text
    output_schema: dict
    repair_error: str | None = None
    previous_output: str | None = None


class LLMBackend(Protocol):
    async def complete(self, request: LLMRequest) -> str:
        """Return JSON. Start a fresh context; never retain/share conversation state."""
        ...


class FakeLLMBackend:
    """Responses keyed by task ID make concurrent tests independent of call order.

    A string returns verbatim (including malformed JSON); exceptions simulate backend
    failures. Each repair consumes the next response for that same task.
    """

    def __init__(self, responses: Mapping[EntityId, Sequence[str | Exception]]):
        self.responses = {key: deque(values) for key, values in responses.items()}
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> str:
        self.requests.append(request.model_copy(deep=True))
        queue = self.responses.get(request.task_id)
        if not queue:
            raise RuntimeError(f"No scripted response for task {request.task_id}")
        response = queue.popleft()
        if isinstance(response, Exception):
            raise response
        return response
