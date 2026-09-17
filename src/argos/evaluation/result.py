"""Strict project-output parsing and arithmetic, without scientific interpretation."""

import json
from typing import Literal

from pydantic import Field

from argos.common import (
    ConstraintCheck,
    EvaluationProtocol,
    FiniteNumber,
    Measurement,
    MetricDirection,
    Model,
    Text,
)
from argos.protocols import MetricComparison


class MetricValue(Model):
    value: float = Field(strict=True, allow_inf_nan=False)
    unit: Text | None = None
    direction: MetricDirection = MetricDirection.INFORMATIONAL


class ConstraintValue(Model):
    passed: bool = Field(strict=True)
    measured_value: FiniteNumber | None = None
    threshold: FiniteNumber | None = None


class EvaluationOutput(Model):
    """JSON printed by the project's command; identity comes from the host."""

    status: Literal["ok"]
    metrics: dict[Text, MetricValue] = Field(min_length=1)
    constraints: dict[Text, bool | ConstraintValue]
    artifacts: list[Text] = Field(default_factory=list)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key.strip() in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        if key != key.strip():
            raise ValueError(f"JSON keys must not have surrounding whitespace: {key!r}")
        result[key] = value
    return result


def parse_output(raw: str, protocol: EvaluationProtocol) -> EvaluationOutput:
    data = json.loads(raw, object_pairs_hook=_unique_object)
    output = EvaluationOutput.model_validate_json(json.dumps(data), strict=True)
    missing_metrics = set(protocol.metric_names) - output.metrics.keys()
    missing_constraints = set(protocol.constraints) - output.constraints.keys()
    if missing_metrics or missing_constraints:
        raise ValueError(
            f"Missing required metrics {sorted(missing_metrics)} or constraints "
            f"{sorted(missing_constraints)}"
        )
    return output


def measurements(output: EvaluationOutput) -> list[Measurement]:
    return [Measurement(name=name, **value.model_dump()) for name, value in output.metrics.items()]


def constraints(output: EvaluationOutput) -> list[ConstraintCheck]:
    return [
        ConstraintCheck(name=name, **({"passed": v} if isinstance(v, bool) else v.model_dump()))
        for name, v in output.constraints.items()
    ]


def compare(current: list[Measurement], baseline: list[Measurement]) -> list[MetricComparison]:
    previous = {m.name: m for m in baseline}
    comparisons = []
    for metric in current:
        old = previous.get(metric.name)
        if old is None:
            continue  # Additional metrics have no baseline comparison.
        if (old.unit, old.direction) != (metric.unit, metric.direction):
            raise ValueError(f"Incompatible baseline unit/direction for {metric.name}")
        delta = metric.value - old.value
        comparisons.append(
            MetricComparison(
                name=metric.name,
                baseline_value=old.value,
                value=metric.value,
                delta=delta,
                percent_change=None if old.value == 0 else delta / abs(old.value) * 100,
                unit=metric.unit,
                direction=metric.direction,
            )
        )
    return comparisons
