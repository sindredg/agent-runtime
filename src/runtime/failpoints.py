"""Failpoints: named places where the worker stops dead, switched on for tests.

A chaos test has to stop a worker at an exact point, for example after a tool
ran and before its result was stored. Killing the container from outside
cannot aim that well. With the FAILPOINT environment variable set to one of
the names below, the worker ends its own process when it reaches that point.

Nothing here does anything unless the variable is set.
"""

import logging
import os
import sys
from collections.abc import Callable

log = logging.getLogger("runtime.worker")

# In the order a job passes them.
NAMES = (
    # The claim is committed and nothing else has happened.
    "after_claim",
    # The `model_request` step is written and the call has not been sent.
    "after_model_request",
    # The first part of the model's answer has arrived.
    "mid_stream",
    # The `tool_intent` step is written and the tool has not started.
    "after_tool_intent",
    # The tool has returned and its result is not stored.
    "after_tool_ran",
    # The `tool_result` step is written.
    "after_tool_result",
)


def die() -> None:
    """End the process at once, as SIGKILL would: no cleanup, exit status 137."""
    sys.stderr.flush()
    os._exit(137)


class Failpoints:
    def __init__(
        self,
        name: str | None = None,
        *,
        tool: str | None = None,
        crash: Callable[[], None] = die,
    ) -> None:
        """`name` is the failpoint that is switched on, or None for none.

        `tool` limits the three tool failpoints to calls of that tool.
        `crash` is what firing does; tests replace it.
        """
        if name is not None and name not in NAMES:
            raise ValueError(f"unknown failpoint {name!r}; the failpoints are {', '.join(NAMES)}")
        self.name = name
        self.tool = tool
        self._crash = crash

    def hit(self, name: str, *, takeovers: int, tool: str | None = None) -> None:
        """The worker has reached the point `name`. Stop here if it is switched on.

        Only on a job that was never taken over. The worker that takes the
        job over passes the same point, and it has to get past it.
        """
        if name != self.name or takeovers != 0:
            return
        if tool is not None and self.tool is not None and tool != self.tool:
            return
        log.warning("failpoint reached, stopping the worker", extra={"failpoint": name})
        self._crash()
