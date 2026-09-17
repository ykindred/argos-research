"""Explicit argv execution with disk logs and POSIX process-group termination."""

import asyncio
import math
import os
import signal
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
        self, argv: list[str], cwd: Path, timeout: float, stdout: Path, stderr: Path
    ) -> ProcessOutcome:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        if os.name != "posix":
            raise RuntimeError("Process-tree termination currently requires POSIX")
        timed_out = invalid = cancelled = False
        code = None
        with stdout.open("wb") as out, stderr.open("wb") as err:
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, stdout=out, stderr=err, start_new_session=True
                )
            except (OSError, ValueError) as exc:
                err.write(str(exc).encode())
                invalid = True
            else:
                try:
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
        record = CommandRecord(
            argv=argv,
            exit_code=code,
            stdout=stdout.read_text(errors="replace"),
            stderr=stderr.read_text(errors="replace"),
        )
        if cancelled:
            raise ProcessCancelled(record)
        return ProcessOutcome(record, timed_out, invalid)
