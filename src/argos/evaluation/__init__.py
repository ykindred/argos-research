"""Objective project measurements and immutable baseline comparison."""

from .baseline import BaselineRunner, approve_baseline
from .evaluator import CommandEvaluator, Evaluator, FakeEvaluator
from .result import EvaluationOutput, compare, parse_output
from .state import record_evaluation

__all__ = [
    "BaselineRunner",
    "CommandEvaluator",
    "EvaluationOutput",
    "Evaluator",
    "FakeEvaluator",
    "approve_baseline",
    "compare",
    "parse_output",
    "record_evaluation",
]
