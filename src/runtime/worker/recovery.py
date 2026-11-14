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


def decide(tool: AnyTool | None, call: ToolCall, *, approved: bool) -> Decision:
    """Decide about an interrupted call. Runs the tool's `check`, so it may block.

    In this order:

    - A person approved the rerun: run it again.
    - The tool is idempotent: run it again.
    - The tool's check answers done: take the result from the check.
    - The check answers not done: run it again.
    - The check answers unknown, fails, or there is none: ask a person.
    """
    if tool is None:
        # Running an unknown tool only yields the "unknown tool" error result.
        return Rerun("the worker has no such tool; running it only reports that")
    if approved:
        return Rerun("a person approved running the call again")
    if tool.idempotent:
        return Rerun("the tool is idempotent")
    # Only a tool that is runtime code can bring a check.
    check = tool.check if isinstance(tool, Tool) else None
    if check is None:
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
