"""Research reasoning roles and bounded independent dispatch (issue #4)."""

from .roles import ManagerPlan, ResearchAgent, ResearchBatch, ResearchDispatcher, ResearchManager
from .runtime import AgentOutcome, StructuredCaller, TaskRecordingError
from .state import ResearchStateWriter

__all__ = [
    "AgentOutcome",
    "ManagerPlan",
    "ResearchAgent",
    "ResearchBatch",
    "ResearchDispatcher",
    "ResearchManager",
    "ResearchStateWriter",
    "StructuredCaller",
    "TaskRecordingError",
]
