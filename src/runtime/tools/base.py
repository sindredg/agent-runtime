"""What a tool is: a name and schema for the model, and what runs a call.

There are two kinds. A `Tool` is a function that runs in the worker, for
tools that are runtime code. A `SandboxTool` (`runtime.tools.sandboxed`)
turns the model's input into a command that runs in a container of its own.
"""

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from runtime.scope import Resolved
    from runtime.tools.sandboxed import SandboxTool


def tool_definition(name: str, description: str, input_schema: dict[str, Any]) -> dict[str, Any]:
    """A tool as the Messages API wants it in `tools`."""
    return {"name": name, "description": description, "input_schema": input_schema}


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What goes back to the model. `is_error` marks a failed call; the job goes on."""

    content: str
    is_error: bool = False
    # True when the output was cut to fit the result. The content says so too.
    truncated: bool = False
    # For a call with a scope: packets its guard dropped. None otherwise.
    dropped_packets: int | None = None


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One call of a tool, as the tool receives it."""

    # The job's workspace directory.
    workspace: Path
    # The input the model gave.
    input: dict[str, Any]
    # Names this call and stays the same when the call is run again after a
    # worker stopped. A tool that changes something outside should store it
    # with the change, or send it as an idempotency key.
    operation_id: str
    # The lease epoch of the worker that runs the call. A sandboxed call
    # labels its container with it.
    lease_epoch: int
    # The job's scope, resolved for this call (ADR 0009). None: no network.
    scope: Resolved | None = None


def operation_id(job_id: object, tool_use_id: str) -> str:
    """The operation id of a call: the job's id and the model's id for the call."""
    return f"{job_id}:{tool_use_id}"


class CheckOutcome(Enum):
    """What a tool's `check` answers when it did not find the call's result."""

    # The call did not happen. The weak answer: only give it when the other
    # system would show the change if it had been made.
    NOT_DONE = "not_done"
    # It cannot be told, for example because the other system is unreachable.
    UNKNOWN = "unknown"


# A check answers with the call's result when the call already happened.
type CheckAnswer = ToolResult | CheckOutcome

# The longest `ends_within` a tool may name: a day, as for a sandboxed call.
LONGEST_TOOL_BOUND = 86_400.0


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # It runs in a thread, so it may block.
    run: Callable[[ToolCall], ToolResult]
    # True when running the call twice does no more than running it once.
    # Such a call is simply run again after a worker stopped in the middle.
    idempotent: bool = False
    # Asks the system behind the tool whether the call already happened. Used
    # only for a call that was interrupted, and only if the tool is not
    # idempotent. Without it a person is asked.
    check: Callable[[ToolCall], CheckAnswer] | None = None
    # True when no call of the tool runs before a person approved it (ADR
    # 0011). A job's spec can ask for the same; it cannot take this away.
    needs_approval: bool = False
    # Seconds after a call starts by which it sends nothing more that changes
    # the system behind it (ADR 0025). The tool keeps to it itself: it runs in
    # a thread, which nobody can stop. A worker that takes the job over waits
    # that long after the intent before it asks `check`. None: no wait.
    ends_within: float | None = None

    def __post_init__(self) -> None:
        bound = self.ends_within
        if bound is not None and not (math.isfinite(bound) and 0 < bound <= LONGEST_TOOL_BOUND):
            raise ValueError(
                f"ends_within must be above 0 and at most {LONGEST_TOOL_BOUND:g}, got {bound!r}"
            )

    def definition(self) -> dict[str, Any]:
        return tool_definition(self.name, self.description, self.input_schema)


# Either kind of tool.
type AnyTool = Tool | SandboxTool


class ToolRegistry:
    """The tools a worker offers, by name."""

    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> AnyTool | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return tuple(tool.definition() for tool in self._tools.values())
