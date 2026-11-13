"""crash_worker: a tool that takes its worker down, for chaos tests only.

It stands in for a tool call that kills the worker every time it runs, for
example by using up all memory. Such a job would be taken over for ever
without a limit on takeovers. The tool is offered only when the worker runs
with CHAOS_TOOLS=1.

Each run is written to a log file before the process ends. The file is the
tool's own record of how often it ran, independent of the job's step log.
"""

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from runtime.tools.base import Tool, ToolCall, ToolResult
from runtime.worker.failpoints import die

LOG_NAME = "crash_worker.log"


def crash_worker_tool(log_dir: Path, *, crash: Callable[[], None] = die) -> Tool:
    """The tool, logging to `log_dir`. `crash` is what ends the process; tests replace it."""

    def run(call: ToolCall) -> ToolResult:
        entry = {"operation_id": call.operation_id, "at": datetime.now(UTC).isoformat()}
        # O_APPEND: workers on several containers write to the one file, a
        # line at a time. fsync: the line must be on disk before the process ends.
        descriptor = os.open(log_dir / LOG_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(descriptor, (json.dumps(entry) + "\n").encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        crash()
        return ToolResult("the worker survived")

    return Tool(
        name="crash_worker",
        description="Stops the worker that runs it. For failure tests.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        run=run,
        # Declared idempotent so that the job is run again without a person
        # after each crash. Otherwise it would stop at the first one.
        idempotent=True,
    )
