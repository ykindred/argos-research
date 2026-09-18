"""One explicit local command adapter; host tools manage their own provider setup.

No credentials are read by ARGOS. An adapter receives JSON on stdin and writes a
single JSON response to stdout. Commands use argv, fresh processes, and deadlines.
"""

import json
from pathlib import Path

from argos.backends.coding import CODING_PROMPT
from argos.execution.process import ProcessRunner
from argos.protocols import CodingResult


def command_argv(argv):
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) or not a for a in argv):
        raise ValueError("Backend command must be a nonempty JSON string array")
    return list(argv)


class CommandLLMBackend:
    def __init__(self, argv, storage: Path, *, timeout=60):
        self.argv = command_argv(argv)
        self.storage = storage.resolve()
        self.timeout = timeout

    async def complete(self, request):
        attempt = (
            self.storage / str(request.task_id) / ("repair" if request.repair_error else "initial")
        )
        attempt.mkdir(parents=True, exist_ok=False)
        source = attempt / "request.json"
        source.write_text(request.model_dump_json())
        result = await ProcessRunner().run(
            self.argv,
            attempt,
            self.timeout,
            attempt / "stdout.log",
            attempt / "stderr.log",
            stdin=source,
        )
        if result.timed_out:
            raise TimeoutError("LLM command deadline exceeded")
        if result.invalid_command or result.record.exit_code != 0:
            raise RuntimeError(f"LLM adapter failed: {result.record.stderr[:2000]}")
        return result.record.stdout


class CommandCodingBackend:
    def __init__(self, argv):
        self.argv = command_argv(argv)

    async def run(self, workspace, task, timeout):
        directory = workspace / ".argos-coding"
        directory.mkdir(exist_ok=True)
        source = directory / "request.json"
        source.write_text(
            json.dumps({"system_prompt": CODING_PROMPT, "task": task.model_dump(mode="json")})
        )
        result = await ProcessRunner().run(
            self.argv,
            workspace,
            timeout,
            directory / "stdout.log",
            directory / "stderr.log",
            stdin=source,
        )
        if result.timed_out:
            raise TimeoutError("Coding command deadline exceeded")
        if result.invalid_command or result.record.exit_code != 0:
            raise RuntimeError(f"Coding adapter failed: {result.record.stderr[:2000]}")
        return CodingResult.model_validate_json(result.record.stdout)
