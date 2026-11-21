"""create_issue: open an issue in an issue tracker.

The one tool here that changes something outside the runtime. It sends the
call's operation id with the issue and brings a check that looks the id up,
so a call that was interrupted is not repeated when the issue already exists.
The tracker is the fake tracker or Gitea (`runtime.tools.trackers`).

The run uses the same lookup when its request got no answer. The tracker may
have stored the issue by then, and a model that is told the call failed may
make it again: a new call, which no operation id connects to the first.

The request that opens the issue has one deadline, from its start to its
answer. Past it the request is given up and its connection closed, so a
worker that takes the job over knows how long to wait before it asks the
tracker (ADR 0025).
"""

import asyncio
import re

import httpx2

from runtime.tools.base import CheckAnswer, CheckOutcome, Tool, ToolCall, ToolResult
from runtime.tools.trackers import Issue, Network, Refused, Tracker

# Seconds for the request that opens an issue: connecting, sending and the answer.
CREATE_WITHIN = 10.0
# Added for the worker that takes the job over: its wait is counted on the
# database's clock from the intent, the deadline on this worker's clock from
# the start of the run.
MARGIN = 2.0

# The characters an operation id may have here. A tracker finds the issue by
# the id, so an id with a space or a line break could match another call's.
_SENDABLE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")


def _created(issue: Issue, title: str) -> ToolResult:
    """The result of a call whose issue exists.

    The number is the tracker's. The title is the call's own: whoever can
    edit the issue in the tracker would otherwise write into the model's
    conversation, through a result nobody is asked about.
    """
    return ToolResult(f"created issue {issue.number}: {title}")


def create_issue_tool(
    tracker: Tracker, *, transport: Network | None = None, create_within: float = CREATE_WITHIN
) -> Tool:
    """The tool, for `tracker`. `transport` replaces the network in tests."""

    async def send(title: str, body: str, operation_id: str) -> Issue:
        # One deadline for the whole request. Past it the request is
        # cancelled and the client closed: nothing more of it leaves here.
        async with asyncio.timeout(create_within), tracker.sender(transport) as http:
            return await tracker.create(http, title, body, operation_id)

    def sendable(call: ToolCall) -> tuple[str, str] | None:
        """The title and body the run sends, or None for input it sends nothing for."""
        title, body = call.input.get("title"), call.input.get("body")
        if not isinstance(title, str) or not title or not isinstance(body, str):
            return None
        return title, body

    def run(call: ToolCall) -> ToolResult:
        sent = sendable(call)
        if sent is None:
            return ToolResult(
                "input must have a non-empty string `title` and a string `body`", True
            )
        title, body = sent
        if not _SENDABLE_ID.fullmatch(call.operation_id):
            return ToolResult("the call's id has characters a tracker cannot match", True)
        try:
            # This runs in a thread of the worker, which has no event loop of its own.
            return _created(asyncio.run(send(title, body, call.operation_id)), title)
        except TimeoutError:
            # Given up at the deadline. The tracker may have stored the issue.
            return look_up(call, f"the tracker did not answer within {create_within:g} s")
        except Refused as error:
            if 400 <= error.status < 500:
                # The tracker refused the request: no issue.
                return ToolResult(str(error), is_error=True)
            # Any other answer, a server error above all, can come after
            # the issue was stored.
            return look_up(call, str(error))
        except httpx2.HTTPError as error:
            return look_up(call, f"the tracker could not be reached: {error!r}")
        except (ValueError, LookupError, TypeError, ArithmeticError, RecursionError) as error:
            return look_up(call, f"the tracker's answer was not an issue: {error!r}")

    def look_up(call: ToolCall, failure: str) -> ToolResult:
        """After an answer that does not say whether the issue exists.

        The request may have arrived. Ask for the issue before telling the
        model that the call failed: told that, it may make a new call, which
        no operation id connects to this one.
        """
        found = check(call)
        if isinstance(found, ToolResult):
            return found
        # Not found is not a no either. The request may still arrive.
        return ToolResult(f"{failure}. The issue may exist all the same.", is_error=True)

    def check(call: ToolCall) -> CheckAnswer:
        """Ask the tracker for an issue with this call's operation id.

        Found: done, with the result the run would have returned. A clear
        answer without such an issue: not done. Anything else is unknown,
        and a person is asked.
        """
        if not _SENDABLE_ID.fullmatch(call.operation_id):
            # The run sent nothing for such an id, but say so only to a person.
            return CheckOutcome.UNKNOWN
        try:
            with tracker.client(transport) as http:
                found = tracker.find(http, call.operation_id)
        except Exception:  # Any failure to ask is an unclear answer.
            return CheckOutcome.UNKNOWN
        if found is None:
            return CheckOutcome.NOT_DONE
        sent = sendable(call)
        # The run sends nothing for such input, so an issue with this id is
        # not this call's. A person is asked.
        return CheckOutcome.UNKNOWN if sent is None else _created(found, sent[0])

    return Tool(
        name="create_issue",
        description="Open an issue in the issue tracker. Returns the number of the new issue.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "One line that names the problem"},
                "body": {"type": "string", "description": "The evidence, for example log lines"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        run=run,
        # Running it twice opens two issues.
        idempotent=False,
        check=check,
        ends_within=create_within + MARGIN,
    )
