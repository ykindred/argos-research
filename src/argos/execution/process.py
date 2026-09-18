"""Explicit argv execution with disk logs and POSIX process-group termination."""

import asyncio
import json
import math
import os
import signal
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from argos.common import CommandRecord


@dataclass
class ProcessOutcome:
    record: CommandRecord
    timed_out: bool = False
    invalid_command: bool = False


class ProcessCancelled(asyncio.CancelledError):
    """Cancellation with the terminated command's actual exit code and output."""

    def __init__(self, record: CommandRecord):
        self.record = record
        super().__init__("Command cancelled")


class ProcessRunner:
    async def run(
        self,
        argv: list[str],
        cwd: Path,
        timeout: float,
        stdout: Path,
        stderr: Path,
        *,
        stdin: Path | None = None,
    ) -> ProcessOutcome:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if os.name != "posix":
            raise RuntimeError("Process-tree termination currently requires POSIX")
        timed_out = invalid = cancelled = False
        code = None
        marker = stdout.with_name(stdout.name + ".process.json")
        with ExitStack() as stack:
            out = stack.enter_context(stdout.open("wb"))
            err = stack.enter_context(stderr.open("wb"))
            source = stack.enter_context(stdin.open("rb")) if stdin else asyncio.subprocess.DEVNULL
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, stdout=out, stderr=err, stdin=source, start_new_session=True
                )
            except (OSError, ValueError) as exc:
                err.write(str(exc).encode())
                invalid = True
            else:
                identity = process_identity(process.pid)
                try:
                    marker.write_text(json.dumps({"pid": process.pid, "identity": identity}))
                    await asyncio.wait_for(process.wait(), timeout)
                except TimeoutError:
                    timed_out = True
                except asyncio.CancelledError:
                    cancelled = True
                finally:
                    # Also stop background children left behind by a successful parent.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    code = process.returncode
                    marker.unlink(missing_ok=True)
        record = CommandRecord(
            argv=argv,
            exit_code=code,
            stdout=stdout.read_text(errors="replace"),
            stderr=stderr.read_text(errors="replace"),
        )
        if cancelled:
            raise ProcessCancelled(record)
        return ProcessOutcome(record, timed_out, invalid)


def process_identity(pid: int):
    """Linux boot/start identity prevents signaling a reused PID after restart."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return [Path("/proc/sys/kernel/random/boot_id").read_text().strip(), stat[19]]
    except FileNotFoundError:
        return None


def recover_processes(directory: Path):
    """Stop only verified orphan groups from this host's retained command markers."""
    for marker in directory.rglob("*.process.json"):
        data = json.loads(marker.read_text())
        pid = data["pid"]
        identity = process_identity(pid)
        if identity is not None and identity == data["identity"]:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif identity is None:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError(
                    "Orphan group leader disappeared; human process inspection needed"
                )
        # A reused PID belongs to another process and must never be signaled.
        marker.unlink()
