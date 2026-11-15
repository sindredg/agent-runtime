"""What to do with a tool call that was interrupted.

A worker that takes a job over can find a `tool_intent` step without a
`tool_result`. The tool may not have started, may have stopped half way, or
may have finished without its result being stored. The log cannot tell
which. The decision is made here, from what the tool says about itself.
"""

from dataclasses import dataclass

from runtime.store.payload import InvalidPayload, check_json
from runtime.tools.base import AnyTool, CheckOutcome, Tool, ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class Rerun:
    """Run the call again, under a new intent step."""

    reason: str


@dataclass(frozen=True, slots=True)
class Recovered:
    """The call already happened. This is its result; the tool does not run."""

    result: ToolResult


@dataclass(frozen=True, slots=True)
class Ask:
    """Nobody can tell whether the call happened. A person decides."""

    reason: str


type Decision = Rerun | Recovered | Ask


# Why a call that needs approval is asked about again after it was interrupted.
_EACH_RUN = "each run of this tool needs a person's approval"


def decide(
    tool: AnyTool | None, call: ToolCall, *, approved: bool, gated: bool = False
) -> Decision:
    """Decide about an interrupted call. Runs the tool's `check`, so it may block.

    In this order:

    - A person approved the rerun: run it again.
    - The tool is idempotent: run it again.
    - The tool's check answers done: take the result from the check.
    - The check answers not done: run it again.
    - The check answers unknown, fails, or there is none: ask a person.

    `gated` says that the call needs approval before it runs (ADR 0011).
    The approval it had was for the run that was interrupted, so nothing
    but a new one lets it run again: where the list says "run it again"
    without a person, a person is asked. A result the check finds is still
    taken, since storing it runs nothing.
    """
    if tool is None:
        # Running an unknown tool only yields the "unknown tool" error result.
        return Rerun("the worker has no such tool; running it only reports that")
    if approved:
        return Rerun("a person approved running the call again")
    if tool.idempotent and not gated:
        return Rerun("the tool is idempotent")
    # Only a tool that is runtime code can bring a check.
    check = tool.check if isinstance(tool, Tool) else None
    if check is None:
        if gated:
            return Ask(f"the call was interrupted, and {_EACH_RUN}")
        return Ask("the tool is not idempotent and has no check")
    try:
        answer = check(call)
    # SystemExit too: a check that calls sys.exit() must not take the worker down.
    except (Exception, SystemExit) as error:
        reason = f"the tool's check failed: {type(error).__name__}: {error}"
        try:
            check_json(reason)
        except InvalidPayload:
            # The error text came from somewhere else and cannot be stored.
            # The question still has to reach the log.
            reason = f"the tool's check failed: {type(error).__name__}"
        return Ask(reason)
    if answer is CheckOutcome.NOT_DONE:
        if gated:
            return Ask(f"the tool's check says the call did not happen, and {_EACH_RUN}")
        return Rerun("the tool's check says the call did not happen")
    if answer is CheckOutcome.UNKNOWN:
        return Ask("the tool's check could not tell whether the call happened")
    if not isinstance(answer, ToolResult) or not isinstance(answer.content, str):
        return Ask("the tool's check gave an answer that is not a result")
    try:
        check_json(answer.content)
    except InvalidPayload as error:
        return Ask(f"the tool's check gave a result that cannot be stored: {error}")
    return Recovered(answer)
