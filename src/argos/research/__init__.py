"""Research reasoning roles and bounded independent dispatch (issue #4)."""

from .critic import Critic, CriticEvidence, CriticInput, CriticState, FakeCritic, LLMCritic
from .roles import ManagerPlan, ResearchAgent, ResearchBatch, ResearchDispatcher, ResearchManager
from .runtime import AgentOutcome, StructuredCaller, TaskRecordingError
from .state import ResearchStateWriter

__all__ = [
    "AgentOutcome",
    "Critic",
    "CriticEvidence",
    "CriticInput",
    "CriticState",
    "FakeCritic",
    "LLMCritic",
    "ManagerPlan",
    "ResearchAgent",
    "ResearchBatch",
    "ResearchDispatcher",
    "ResearchManager",
    "ResearchStateWriter",
    "StructuredCaller",
    "TaskRecordingError",
]
