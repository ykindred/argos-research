"""Minimal trusted human CLI. Backends are explicit local host adapters."""

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

from argos import models as m
from argos.backends.coding import FakeCodingBackend
from argos.backends.llm import FakeLLMBackend
from argos.core.host import build_runtime, refresh_baseline, runner_lock
from argos.core.orchestrator import now
from argos.project import ProjectLoader
from argos.state import StateError, StateStore
from argos.state.store import TYPES


def parser():
    root = argparse.ArgumentParser(prog="argos")
    root.add_argument("--project", type=Path, default=Path("."))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    run = commands.add_parser("run")
    run.add_argument(
        "--backend-command",
        required=True,
        help="JSON argv: LLMRequest on stdin, JSON output on stdout",
    )
    run.add_argument(
        "--coding-command", help="JSON argv: CodingTask on stdin, CodingResult on stdout"
    )
    run.add_argument("--fake-files", type=Path, help="Offline coding file mapping JSON")
    run.add_argument("--steps", type=int)
    commands.add_parser("status")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("id", type=UUID)
    commands.add_parser("pause")
    commands.add_parser("resume")
    answer = commands.add_parser("answer")
    answer.add_argument("gate", type=UUID)
    answer.add_argument("text")
    answer.add_argument("--approve", action="store_true")
    answer.add_argument("--edit-question")
    baseline = commands.add_parser("baseline")
    baseline.add_argument("action", choices=["refresh"])
    baseline.add_argument("--reason", required=True)
    return root


def emit(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, indent=2, default=str))


def main(argv=None):
    args = parser().parse_args(argv)
    directory = args.project.resolve()
    storage = directory / ".argos"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        with StateStore(storage / "state.sqlite") as store:
            if args.command == "init":
                with runner_lock(storage):
                    if store.list(m.Project):
                        raise StateError("Already initialized; use baseline refresh explicitly")
                    config = ProjectLoader().load(directory)
                    if storage.is_relative_to(Path(config.source_repository)):
                        raise StateError("Project state must be outside the source checkout")
                    project = m.Project(
                        id=uuid4(),
                        created_at=now(),
                        name=directory.name,
                        config=config,
                        status="paused",
                    )
                    store.create(project)
                    project = store.approve_main_question(
                        project.id,
                        config.main_research_question,
                        rationale="Human initialized this project via CLI",
                    )
                    asyncio.run(
                        refresh_baseline(
                            store, project, storage, rationale="Human initialized clean baseline"
                        )
                    )
                    emit(store.get(m.Project, project.id))
                return 0
            projects = store.list(m.Project)
            if len(projects) != 1:
                raise StateError("Run argos init first (one project per directory)")
            project = projects[0]
            if args.command == "status":
                emit(
                    {
                        "project": project.model_dump(mode="json"),
                        "runtime": store.runtime_get(project.id, "cursor"),
                        "snapshot": store.snapshot(project.id, limit=8).model_dump(mode="json"),
                    }
                )
                return 0
            if args.command == "inspect":
                for cls in TYPES.values():
                    try:
                        entity = store.get(cls, args.id)
                    except KeyError:
                        continue
                    emit(
                        {
                            "entity": entity.model_dump(mode="json"),
                            "history": [
                                e.model_dump(mode="json") for e in store.history(cls, args.id)
                            ],
                        }
                    )
                    return 0
                raise StateError("Unknown entity ID")
            llm, coding = FakeLLMBackend({}), FakeCodingBackend()
            if args.command == "run":
                if store.runtime_get(project.id, "baseline_cursor") or project.baseline_id is None:
                    raise StateError("Establish/recover the baseline with baseline refresh first")
                from argos.backends.command import CommandCodingBackend, CommandLLMBackend

                llm = CommandLLMBackend(
                    json.loads(args.backend_command),
                    storage / "llm",
                    timeout=project.config.resource_limits.timeout_seconds,
                )
                if args.coding_command:
                    coding = CommandCodingBackend(json.loads(args.coding_command))
                elif args.fake_files:
                    coding = FakeCodingBackend(json.loads(args.fake_files.read_text()))
                elif args.backend_command:
                    raise StateError(
                        "Real runs require --coding-command (or explicit --fake-files)"
                    )
            runtime = build_runtime(store, project, storage, llm, coding)
            if args.command == "pause":
                runtime.pause()  # May be requested while a runner owns the lock.
                return 0
            with runner_lock(storage):
                if args.command == "run":
                    emit(asyncio.run(runtime.run(max_transitions=args.steps)))
                elif args.command == "resume":
                    runtime.resume()
                elif args.command == "answer":
                    runtime.answer(
                        args.gate,
                        args.text,
                        approve=args.approve,
                        edited_question=args.edit_question,
                    )
                elif args.command == "baseline":
                    if (
                        runtime.state.operation
                        or runtime.state.actions
                        or runtime.state.evaluation_run
                    ):
                        raise StateError("Finish or recover pending work before baseline refresh")
                    emit(
                        asyncio.run(
                            refresh_baseline(store, project, storage, rationale=args.reason)
                        )
                    )
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(f"argos: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
