"""Offline, scripted example; scientific choices are fixture data, not core heuristics."""

import argparse
import asyncio
import json
import shutil
import subprocess
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4, uuid5

from argos import models as m
from argos.backends import FakeLLMBackend
from argos.backends.coding import FakeCodingBackend
from argos.core.host import build_runtime, refresh_baseline, runner_lock
from argos.project import ProjectLoader
from argos.state import StateStore

OPTIMIZED = """def count_distinct(values):
    seen = set()
    comparisons = 0
    for value in values:
        comparisons += 1
        seen.add(value)
    return len(seen), comparisons
"""


class SyntheticLLM(FakeLLMBackend):
    def __init__(self):
        super().__init__({})

    async def complete(self, request):
        context = json.loads(request.context_json)
        if request.role == "research_agent":
            response = dict(
                task_id=context["task"]["id"],
                agent_id=context["agent_id"],
                created_at=datetime.now(UTC).isoformat(),
                ideas=[],
                summary="Inspect set membership; falsify with duplicate and negative values.",
            )
        elif request.role == "critic":
            response = dict(
                claim_id=context["claim"]["id"],
                created_at=datetime.now(UTC).isoformat(),
                verdict="needs_more_evidence",
                measurement_comparability="comparable",
                main_question_support="uncertain",
                assessment_rationale="Fixed workload measured; broader support unresolved",
                rationale="One fixed workload is insufficient.",
                weaknesses=["Limited workload coverage"],
                risks=[],
                requested_checks=["Repeat on a larger duplicate-heavy workload"],
            )
        else:
            project, frontier = context["project"], context["frontier"]
            config = project["config"]
            subproblems = [
                s
                for s in frontier["active_subproblems"]
                if s["question"] != "Human baseline measurement"
            ]
            hypotheses = [
                h
                for h in frontier["hypotheses"]
                if h["statement"] != "Measure the unchanged source"
            ]
            experiments = [
                e for e in frontier["recent_experiments"] if e["goal"] != "Measure clean baseline"
            ]
            if not subproblems:
                action = dict(action_type="create_subproblem", question="Reduce membership work")
            elif not frontier["active_branches"]:
                perspectives = ["source analysis", "complexity", "falsification"]
                action = dict(
                    action_type="dispatch_research_agents",
                    tasks=[
                        dict(
                            id=str(uuid5(request.task_id, perspective)),
                            project_id=project["id"],
                            subproblem_id=subproblems[0]["id"],
                            branch_id=str(uuid4()),
                            question="Can set membership reduce redundant comparisons?",
                            main_research_question=config["main_research_question"],
                            context="Integer equality only; preserve fixed correctness tests.",
                            perspective=perspective,
                            evidence=[],
                        )
                        for perspective in perspectives
                    ],
                )
            elif context["exploration"]:
                action = dict(
                    action_type="create_hypothesis",
                    subproblem_id=subproblems[0]["id"],
                    statement="A set reduces membership operations for this fixed workload.",
                    rationale="Independent source/complexity/falsification results joined.",
                )
            elif not experiments:
                action = dict(
                    action_type="propose_experiment",
                    spec=dict(
                        experiment_id=str(uuid4()),
                        hypothesis_id=hypotheses[0]["id"],
                        hypothesis_statement=hypotheses[0]["statement"],
                        goal="Measure set membership",
                        prediction="Fewer membership operations",
                        success_criteria=["Exact results, fewer operations"],
                        failure_criteria=["Incorrect count or no reduction"],
                        baseline_id=project["baseline_id"],
                        requested_change="Use a set for membership",
                        build_steps=[config["build_command"]],
                        test_steps=[config["test_command"]],
                        run_steps=[config["test_command"]],
                        evaluation_protocol=config["evaluation_protocol"],
                        resource_limits=config["resource_limits"],
                        scope=config["scope"],
                    ),
                )
            elif experiments[0]["status"] == "planned":
                action = dict(
                    action_type="implement_experiment", experiment_id=experiments[0]["id"]
                )
            elif experiments[0]["status"] != "completed":
                action = dict(
                    action_type="stop", reason="Failure recorded; no scientific conclusion"
                )
            elif not frontier["candidate_claims"]:
                observation = next(
                    o
                    for o in frontier["recent_observations"]
                    if o["evaluation"]["experiment_id"] == experiments[0]["id"]
                )
                action = dict(
                    action_type="form_claim",
                    statement="Membership operations fell on this workload.",
                    evidence=dict(
                        supporting_observation_ids=[observation["id"]],
                        contradicting_observation_ids=[],
                    ),
                )
            elif any(t["kind"] == "critic" for t in context["failed_tasks"]):
                action = dict(action_type="stop", reason="Critic failure retained; review pending")
            elif not frontier["reviews"]:
                action = dict(
                    action_type="request_critic_review",
                    claim_id=frontier["candidate_claims"][0]["id"],
                )
            else:
                action = dict(
                    action_type="stop", reason="Bounded demo ends with Critic checks pending"
                )
            response = dict(
                summary="Scripted synthetic step",
                actions=[
                    dict(
                        project_id=project["id"],
                        rationale="Offline E2E fixture decision",
                        action=action,
                    )
                ],
            )
        self.responses[request.task_id] = deque([json.dumps(response)])
        return await super().complete(request)


def prepare(destination):
    """Creates commits only in a new disposable synthetic fixture repository."""
    shutil.copytree(Path(__file__).parent / "repo", destination / "repo")
    for name in ["research.md", "project.yaml"]:
        shutil.copyfile(Path(__file__).parent / name, destination / name)
    repo = destination / "repo"
    for args in [
        ("init",),
        ("add", "."),
        (
            "-c",
            "user.name=ARGOS fixture",
            "-c",
            "user.email=fixture@localhost",
            "commit",
            "-m",
            "Synthetic baseline",
        ),
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return ProjectLoader().load(destination)


async def demo(destination):
    config = prepare(destination)
    storage = destination / ".argos"
    storage.mkdir()
    with runner_lock(storage), StateStore(storage / "state.sqlite") as store:
        project = store.create(
            m.Project(
                id=uuid4(),
                created_at=datetime.now(UTC),
                name="Synthetic distinct count",
                config=config,
            )
        )
        project = store.approve_main_question(
            project.id, config.main_research_question, rationale="Human invoked the synthetic demo"
        )
        await refresh_baseline(
            store, project, storage, rationale="Synthetic baseline initialization"
        )
        runtime = build_runtime(
            store,
            store.get(m.Project, project.id),
            storage,
            SyntheticLLM(),
            FakeCodingBackend({"algorithm.py": OPTIMIZED}),
        )
        await runtime.run()
        print(
            json.dumps(
                {
                    "project": str(project.id),
                    "status": runtime.project.status,
                    "cycles": runtime.state.cycle,
                    "observations": len(store.list(m.Observation, project_id=project.id)),
                    "reviews": len(store.reviews(project.id)),
                    "evidence": str(storage),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "destination", type=Path, help="New disposable directory, outside ARGOS source"
    )
    asyncio.run(demo(parser.parse_args().destination))
