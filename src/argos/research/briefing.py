"""Compact event briefing, without replaying execution logs or conversation history."""


class BriefingBuilder:
    def __init__(self, *, budget=None):
        self.budget = budget

    def build(self, snapshot, *, event, batch=None):
        fields = (
            "active_subproblems",
            "active_branches",
            "hypotheses",
            "recent_observations",
            "candidate_claims",
            "failed_directions",
            "abandoned_branches",
            "rejected_claims",
            "reviews",
        )
        data = snapshot.model_dump(mode="json")
        frontier = {key: data[key][:8] for key in fields}
        frontier["truncated"] = sorted(
            set(snapshot.truncated) | {key for key in fields if len(data[key]) > 8}
        )
        frontier["failed_runs"] = [
            {
                "id": str(run.id),
                "experiment_id": str(run.experiment_id),
                "failure": run.result.failure.model_dump(mode="json"),
            }
            for run in snapshot.failed_runs[:8]
            if run.result and run.result.failure
        ]
        frontier["recent_experiments"] = [
            {
                "id": str(exp.id),
                "hypothesis_id": str(exp.spec.hypothesis_id),
                "status": exp.status,
                "goal": exp.spec.goal,
                "allowed_actions": ["implement_experiment"] if exp.status == "planned" else [],
                "retry_of": str(exp.spec.retry_of) if exp.spec.retry_of else None,
            }
            for exp in snapshot.recent_experiments[:8]
        ]
        frontier["decisions"] = [
            {
                "id": str(decision.id),
                "summary": decision.summary,
                "rationale": decision.rationale,
                "references": [ref.model_dump(mode="json") for ref in decision.references],
            }
            for decision in snapshot.decisions[:5]
        ]
        for key, limit in (("failed_runs", 8), ("recent_experiments", 8), ("decisions", 5)):
            if len(data[key]) > limit and key not in frontier["truncated"]:
                frontier["truncated"].append(key)
        for observation in frontier["recent_observations"]:
            provenance = observation["evaluation"].get("provenance")
            if provenance and provenance.get("command"):
                command = provenance["command"]
                provenance["command"] = {**command, "stdout": "", "stderr": ""}
        return {
            "event": event,
            "budget": self.budget() if self.budget else None,
            "project": data["project"],
            "frontier": frontier,
            "exploration": batch.model_dump(mode="json") if batch else None,
            "failed_tasks": [
                {"id": str(task.id), "kind": task.kind, "failure": task.failure}
                for task in snapshot.tasks[:8]
                if task.failure
            ],
        }
