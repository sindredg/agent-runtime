"""Tools whose calls run as a command in a container of their own (ADR 0008).

A sandboxed tool only turns the model's input into the command's arguments.
Everything else about the container is fixed by the sandbox's settings.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from runtime.sandbox.docker import SandboxError
from runtime.sandbox.runner import NotStarted, Run, SandboxSettings
from runtime.tools.base import ToolCall, ToolResult, tool_definition


class Runner(Protocol):
    """What a sandboxed tool needs: `runtime.sandbox.runner.Sandbox`, or a stand-in in tests."""

    @property
    def settings(self) -> SandboxSettings: ...

    async def run(self, job_id: UUID, argv: Sequence[str]) -> Run: ...


# The command's arguments, or an error result for input that cannot be used.
type Command = Callable[[dict[str, Any]], list[str] | ToolResult]


def _with_notes(body: str, notes: list[str]) -> str:
    if body and not body.endswith("\n"):
        body += "\n"
    return body + "\n".join(notes)


def describe(run: Run, *, timeout: float, limit: int) -> ToolResult:
    """The result the model gets for a run: its output, then what happened.

    The content is valid text that can be stored: invalid UTF-8 and NUL
    bytes become U+FFFD. It is at most `limit` bytes as UTF-8, notes
    included. Output that does not fit whole is cut, and the result says so
    and is flagged `truncated`.
    """
    text = run.output.decode("utf-8", "replace").replace("\x00", "�")
    if run.exit_code is None:
        status = f"[stopped after {timeout:g} s: the command did not finish]"
    else:
        status = f"[exit status {run.exit_code}]"
    cut = f"[output cut to fit {limit} bytes; the command wrote {run.total}]"
    truncated = run.truncated
    content = _with_notes(text, [cut, status] if truncated else [status])
    if len(content.encode()) > limit:
        # Replacement characters take three bytes for one that was invalid,
        # so output under the limit can still be too long as text.
        truncated = True
        room = limit - len(_with_notes("", [cut, status]).encode()) - 1
        body = text.encode()[: max(room, 0)].decode("utf-8", "ignore")
        content = _with_notes(body, [cut, status])
    return ToolResult(content, is_error=run.exit_code != 0, truncated=truncated)


@dataclass(frozen=True, slots=True)
class SandboxTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    command: Command
    sandbox: Runner
    # As for `Tool`: True when running the call twice does no more than once.
    idempotent: bool = False

    def definition(self) -> dict[str, Any]:
        return tool_definition(self.name, self.description, self.input_schema)

    async def run(self, call: ToolCall) -> ToolResult:
        """Run the call's command in the job's workspace. A failure is an error result."""
        argv = self.command(call.input)
        if isinstance(argv, ToolResult):
            return argv
        try:
            # The job's directory of the volume is named by its id
            # (`runtime.worker.workspace`).
            job_id = UUID(call.workspace.name)
        except ValueError:
            return ToolResult("the workspace is not a job's workspace", is_error=True)
        try:
            run = await self.sandbox.run(job_id, argv)
        except NotStarted as error:
            return ToolResult(f"the sandbox could not start the command: {error}", True)
        except SandboxError as error:
            # Docker may have started the command before it failed.
            return ToolResult(f"the sandbox failed: {error}. The command may have run.", True)
        settings = self.sandbox.settings
        return describe(run, timeout=settings.timeout, limit=settings.output_limit)
