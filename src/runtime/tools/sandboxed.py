"""Tools whose calls run as a command in a container of their own (ADR 0008).

A sandboxed tool only turns the model's input into the command's arguments.
Everything else about the container is fixed by the sandbox's settings.

With a workspace limit set, the worker measures the job's workspace before
the call and while it runs (ADR 0014). Over the limit, the command does not
start, or is stopped. A workspace that cannot be measured counts as over.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from runtime.sandbox.docker import SandboxError
from runtime.sandbox.runner import MEASURE_WITHIN, NotStarted, Run, SandboxSettings
from runtime.sandbox.workspace import Bounds, OverLimit, Unmeasurable, workspace_size
from runtime.scope import Resolved
from runtime.tools.base import ToolCall, ToolResult, tool_definition


class Runner(Protocol):
    """What a sandboxed tool needs: `runtime.sandbox.runner.Sandbox`, or a stand-in in tests."""

    @property
    def settings(self) -> SandboxSettings: ...

    async def run(
        self, job_id: UUID, lease_epoch: int, argv: Sequence[str], scope: Resolved | None = None
    ) -> Run: ...

    async def remove_older(self, job_id: UUID, lease_epoch: int) -> int: ...


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
    after = [status]
    if run.dropped:
        # The model learns that the scope stopped something, not what.
        after.append(f"[{run.dropped} packets to addresses outside the job's scope were dropped]")
    truncated = run.truncated
    content = _with_notes(text, [cut, *after] if truncated else after)
    if len(content.encode()) > limit:
        # Replacement characters take three bytes for one that was invalid,
        # so output under the limit can still be too long as text.
        truncated = True
        room = limit - len(_with_notes("", [cut, *after]).encode()) - 1
        body = text.encode()[: max(room, 0)].decode("utf-8", "ignore")
        content = _with_notes(body, [cut, *after])
    return ToolResult(
        content, is_error=run.exit_code != 0, truncated=truncated, dropped_packets=run.dropped
    )


def _bounds(settings: SandboxSettings, limit: int) -> Bounds:
    deadline = time.monotonic() + MEASURE_WITHIN
    return Bounds(size=limit, entries=settings.workspace_entries, deadline=deadline)


async def _over(workspace: Path, settings: SandboxSettings, limit: int) -> str | None:
    """Why the workspace is over its limits, or None while it is within them.

    The walk is bounded by MEASURE_WITHIN, so this ends in about that time.
    """
    try:
        await asyncio.to_thread(workspace_size, workspace, _bounds(settings, limit))
    except OverLimit as error:
        return f"the job's workspace {error}"
    except Unmeasurable as error:
        return f"the job's workspace cannot be measured ({error})"
    return None


async def _watched(
    run: Awaitable[Run], workspace: Path, settings: SandboxSettings, limit: int
) -> Run | str:
    """Await `run` while measuring the workspace every `watch_interval` seconds.

    Returns why it was stopped instead, once the workspace is over its
    limits. Stopping cancels the run, and the sandbox kills and removes the
    container before the cancel completes. A run that ends while a
    measurement is under way is returned at once.
    """
    task = asyncio.ensure_future(run)
    measuring: asyncio.Future[str | None] | None = None
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=settings.watch_interval)
            if done:
                return task.result()
            measuring = asyncio.ensure_future(_over(workspace, settings, limit))
            await asyncio.wait({task, measuring}, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                return task.result()
            reason = measuring.result()
            if reason is not None:
                return reason
    finally:
        if measuring is not None and not measuring.done():
            # The walk's thread ends by its own deadline.
            measuring.cancel()
        if not task.done():
            task.cancel()
            # Not suppressed: a cancel of this call that arrives while the
            # container is removed propagates.
            await asyncio.wait({task})
        if not task.cancelled():
            # Retrieved, so asyncio does not report it as never retrieved.
            task.exception()


@dataclass(frozen=True, slots=True)
class SandboxTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    command: Command
    sandbox: Runner
    # As for `Tool`: True when running the call twice does no more than once.
    idempotent: bool = False
    # True when the call gets the job's scope. False: never any network.
    networked: bool = False
    # As for `Tool`: True when no call runs before a person approved it.
    needs_approval: bool = False

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
        settings = self.sandbox.settings
        limit = settings.workspace_limit
        if limit is not None and (reason := await _over(call.workspace, settings, limit)):
            return ToolResult(f"{reason}. The command did not start.", is_error=True)
        try:
            scope = call.scope if self.networked else None
            started = self.sandbox.run(job_id, call.lease_epoch, argv, scope)
            if limit is None:
                run = await started
            else:
                watched = await _watched(started, call.workspace, settings, limit)
                if isinstance(watched, str):
                    return ToolResult(
                        f"[stopped: {watched}. The files it wrote stay; its output is lost.]",
                        is_error=True,
                    )
                run = watched
        except NotStarted as error:
            return ToolResult(f"the sandbox could not start the command: {error}", True)
        except SandboxError as error:
            # Docker may have started the command before it failed.
            return ToolResult(f"the sandbox failed: {error}. The command may have run.", True)
        return describe(run, timeout=settings.timeout, limit=settings.output_limit)
