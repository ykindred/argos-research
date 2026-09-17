"""Trusted host helpers: atomic measurement attachment, never agent tools."""

from uuid import uuid5

from argos.models import Experiment, Observation, Run
from argos.protocols import EvaluatorResult
from argos.state import StateError, StateStore


def record_evaluation(store: StateStore, evaluation: EvaluatorResult) -> Observation | None:
    """Attach once; successful constraints or failed constraints both yield neutral evidence.

    Invalid output stays on the Run with diagnostics and creates no Observation.
    Replaying the same result is idempotent. The caller persists execution first.
    """
    evaluation = EvaluatorResult.model_validate_json(evaluation.model_dump_json())
    with store.transaction():
        run = store.get(Run, evaluation.run_id)
        experiment = store.get(Experiment, run.experiment_id)
        if run.evaluation is not None and run.evaluation != evaluation:
            raise StateError("Run already has a different evaluation")
        run = store.update(Run.model_validate({**run.model_dump(), "evaluation": evaluation}))
        if experiment.status == "running":
            experiment.status = "evaluating"
            experiment = store.update(experiment)
        terminal = "completed" if evaluation.status == "ok" else "invalid_result"
        if evaluation.failure and evaluation.failure.kind == "timeout":
            terminal = "timeout"
        if experiment.status == "evaluating":
            experiment.status = terminal
            store.update(experiment)
        elif experiment.status != terminal:
            raise StateError("Experiment must be running or evaluating before evaluation")
        if evaluation.status != "ok":
            return None
        ident = uuid5(run.id, "objective-observation")
        try:
            return store.get(Observation, ident)
        except KeyError:
            pass
        parts = [
            f"{m.name} = {m.value:g}" + (f" {m.unit}" if m.unit else "")
            for m in evaluation.measurements
        ]
        for c in evaluation.comparisons:
            percent = (
                "undefined (zero baseline)"
                if c.percent_change is None
                else (f"{c.percent_change:+g}%")
            )
            parts.append(f"{c.name}: baseline {c.baseline_value:g}, delta {c.delta:+g} ({percent})")
        parts.extend(
            f"constraint {c.name}: {'passed' if c.passed else 'failed'}"
            for c in evaluation.constraint_checks
        )
        return store.create(
            Observation(
                id=ident,
                created_at=evaluation.evaluated_at,
                run_id=run.id,
                evaluation=evaluation,
                summary="; ".join(parts),
                relation="neutral",
            )
        )
