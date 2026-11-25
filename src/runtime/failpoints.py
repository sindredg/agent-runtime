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
    # The job's first `summary` step is stored.
    "after_summary",
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
        summary: bool = False,
        crash: Callable[[], None] = die,
    ) -> None:
        """`name` is the failpoint that is switched on, or None for none.

        `tool` limits the three tool failpoints to calls of that tool.
        `summary` limits `after_model_request` to a summary request.
        `crash` is what firing does; tests replace it.
        """
        if name is not None and name not in NAMES:
            raise ValueError(f"unknown failpoint {name!r}; the failpoints are {', '.join(NAMES)}")
        self.name = name
        self.tool = tool
        self.summary = summary
        self._crash = crash

    def hit(
        self,
        name: str,
        *,
        takeovers: int,
        tool: str | None = None,
        summaries: int = 0,
        summary: bool = False,
    ) -> None:
        """The worker has reached the point `name`. Stop here if it is switched on.

        Only on a job that was never taken over. The worker that takes the
        job over passes the same point, and it has to get past it.
        `after_summary` goes by `summaries` instead: the number of `summary`
        steps in the job's log once the one just stored is in it.

        `summary` says the request just written is a summary request.
        """
        if name != self.name:
            return
        if name == "after_summary":
            # The log makes this one fire once: the worker that takes the job
            # over finds the first summary stored and counts on from it. So
            # the takeover rule is left out, and a lease lost before the
            # first summary cannot cancel the stop.
            if summaries != 1:
                return
        elif takeovers != 0:
            return
        if name == "after_model_request" and self.summary and not summary:
            return
        if tool is not None and self.tool is not None and tool != self.tool:
            return
        log.warning("failpoint reached, stopping the worker", extra={"failpoint": name})
        self._crash()
