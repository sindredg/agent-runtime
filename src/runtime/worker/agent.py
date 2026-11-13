"""The agent loop: run one claimed job until this worker is done with it.

Each pass reads the step log back as a conversation (replay), does the one
thing that comes next, and writes it down as a step: a model call, or the
tool calls the model asked for.

The worker holds the job through a lease. A heartbeat renews it while the
job runs, and every write is fenced by the lease epoch. A worker that finds
its lease gone, or cannot reach the database, drops the job: it writes
nothing more and leaves the status and the workspace to whoever holds the
job next.

Not built yet: budget and deadline are not enforced.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse, Provider, ProviderError
from runtime.spec import JobSpec
from runtime.store import held
from runtime.store.held import LeaseLost
from runtime.store.jobs import Job
from runtime.store.payload import InvalidPayload, check_json
from runtime.store.status import FINAL_STATUSES, JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import ToolRegistry, ToolResult
from runtime.worker.lease import Lease, keep_renewing
from runtime.worker.replay import replay
from runtime.worker.workspace import prepare_workspace, remove_workspace

log = logging.getLogger("runtime.worker")

# Stop reasons that end a job well. Any other reason without tool calls
# to run is a failure.
_FINISHED = frozenset({"end_turn", "stop_sequence"})
_FAILURES = {
    "max_tokens": "the model's response was cut off at max_tokens",
    "refusal": "the model declined the request",
}


@dataclass(frozen=True, slots=True)
class AgentSettings:
    default_model: str
    max_tokens: int
    workspace_root: Path
    # How long a claim or a renewal holds the job, and how often to renew.
    lease: timedelta = timedelta(seconds=30)
    heartbeat: float = 10.0


class _JobFailed(Exception):
    """The job cannot go on. The message becomes the job's error."""


class _StoreUnreachable(Exception):
    """A read or write of the job could not be sent to the database."""


async def _stored[T](call: Awaitable[T]) -> T:
    """Await a call to the store. A database or network error becomes _StoreUnreachable.

    LeaseLost, StepConflict and errors raised before anything is sent pass
    through unchanged: they are answers, not a failure to ask.
    """
    try:
        return await call
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        raise _StoreUnreachable(f"{type(error).__name__}: {error}") from error


class _StepWriter:
    """Appends steps for one job with rising `seq`. Every step is one fenced statement."""

    def __init__(self, engine: AsyncEngine, lease: Lease, steps: list[Step]) -> None:
        self._engine = engine
        self._lease = lease
        self.steps = steps

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        seq = self.steps[-1].seq + 1 if self.steps else 1
        # One statement for the step and its tokens: the count and the log
        # cannot disagree.
        step = await _stored(
            held.append_step(
                self._engine,
                job_id=self._lease.job_id,
                lease_epoch=self._lease.epoch,
                seq=seq,
                kind=kind,
                payload=payload,
                tokens=tokens,
            )
        )
        self.steps.append(step)
        log.info("step", extra={"job_id": self._lease.job_id, "seq": seq, "kind": kind.value})
        return step

    async def reread(self) -> None:
        """Read the log again, when the writer's idea of the next seq may be wrong."""
        async with self._engine.connect() as conn:
            self.steps = await read_steps(conn, self._lease.job_id)


async def _run_tool(tools: ToolRegistry, workspace: Path, call: dict[str, Any]) -> ToolResult:
    """Run one tool call. A failure of the tool becomes an error result for the model."""
    tool = tools.get(call["name"])
    if tool is None:
        return ToolResult(f"unknown tool: {call['name']}", is_error=True)
    try:
        result = await asyncio.to_thread(tool.run, workspace, call["input"])
    # SystemExit too: a tool that calls sys.exit() must not take the worker down.
    except (Exception, SystemExit) as error:
        log.exception("tool raised", extra={"tool": call["name"]})
        result = ToolResult(f"the tool failed: {error}", is_error=True)
    if not isinstance(result, ToolResult) or not isinstance(result.content, str):
        return ToolResult("the tool returned no text result", is_error=True)
    try:
        check_json(result.content)
    except InvalidPayload as error:
        return ToolResult(f"the tool's output cannot be stored: {error}", is_error=True)
    return result


def _response_problem(response: ModelResponse) -> str | None:
    """What is wrong with a response's shape, or None. Checked before any tool runs."""
    if not response.stop_reason:
        return "it has no stop_reason"
    content: Any = response.content
    if not isinstance(content, list) or not all(isinstance(block, dict) for block in content):
        return "its content is not a list of blocks"
    ids = []
    for block in content:
        if block.get("type") != "tool_use":
            continue
        tool_use_id, name = block.get("id"), block.get("name")
        if not (isinstance(tool_use_id, str) and tool_use_id and isinstance(name, str)):
            return "a tool_use block has no id or no name"
        if not isinstance(block.get("input"), dict):
            return "a tool_use block's input is not an object"
        ids.append(tool_use_id)
    if len(set(ids)) != len(ids):
        return "two tool_use blocks have the same id"
    return None


async def _loop(
    writer: _StepWriter,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    spec: JobSpec,
    settings: AgentSettings,
    workspace: Path,
    lease: Lease,
) -> None:
    """Run until the model ends its turn. Raises _JobFailed if the job cannot finish.

    Raises LeaseLost as soon as the worker can no longer rely on its lease.
    """
    model = spec.model or settings.default_model
    while True:
        state = replay(spec.prompt, writer.steps)

        if state.needs_model_call:
            request = ModelRequest(
                model=model,
                max_tokens=settings.max_tokens,
                messages=state.messages,
                system=spec.system,
                tools=tools.definitions(),
                user_id=str(job.id),
            )
            lease.check()
            await writer.append(
                StepKind.MODEL_REQUEST,
                {"model": model, "max_tokens": request.max_tokens, "messages": len(state.messages)},
            )
            # Immediately before the call: a worker that was paused past its
            # lease must not send it.
            lease.check()
            try:
                response = await provider.complete(request)
            except ProviderError as error:
                raise _JobFailed(f"model call failed: {error}") from error
            payload: dict[str, Any] = {
                "id": response.id,
                "content": response.content,
                "stop_reason": response.stop_reason,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                },
            }
            problem = _response_problem(response)
            try:
                check_json(payload)
            except InvalidPayload as error:
                # The tokens were spent, so the step and the count are still
                # written. Only the content that cannot be stored is left out.
                problem = problem or f"it cannot be stored: {error}"
                payload["content"] = []
                payload["content_dropped"] = True
            await writer.append(
                StepKind.MODEL_RESPONSE,
                payload,
                tokens=response.input_tokens + response.output_tokens,
            )
            if problem:
                raise _JobFailed(f"the model's response is not usable: {problem}")
            continue

        # A response cut off at max_tokens can hold half a tool call, so the
        # stop reason is checked before any tool runs.
        if state.stop_reason == "tool_use" and state.pending:
            for call in state.pending:
                # Before the intent: a call that will not start must not
                # leave an intent behind, which would look interrupted.
                lease.check()
                await writer.append(
                    StepKind.TOOL_INTENT,
                    {"tool_use_id": call["id"], "name": call["name"], "input": call["input"]},
                )
                # And immediately before the run, in case the write was slow.
                lease.check()
                result = await _run_tool(tools, workspace, call)
                await writer.append(
                    StepKind.TOOL_RESULT,
                    {
                        "tool_use_id": call["id"],
                        "content": result.content,
                        "is_error": result.is_error,
                    },
                )
            continue

        if state.stop_reason in _FINISHED:
            return
        raise _JobFailed(
            _FAILURES.get(state.stop_reason or "", f"unexpected stop_reason: {state.stop_reason}")
        )


async def _run(
    engine: AsyncEngine,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    settings: AgentSettings,
    lease: Lease,
) -> JobStatus:
    """Run the job and record its outcome. Returns the status it was left in.

    Raises LeaseLost or _StoreUnreachable when the worker has to drop the job.
    """
    async with engine.connect() as conn:
        steps = await _stored(read_steps(conn, job.id))
    writer = _StepWriter(engine, lease, steps)
    failure: str | None = None
    try:
        spec = JobSpec.model_validate(job.spec)
        workspace = await asyncio.to_thread(
            prepare_workspace, settings.workspace_root, job.id, spec.files
        )
        await _loop(writer, provider, tools, job, spec, settings, workspace, lease)
    except _JobFailed as error:
        failure = str(error)
    except LeaseLost, _StoreUnreachable:
        raise
    except Exception as error:
        log.exception("job crashed", extra={"job_id": job.id})
        failure = f"{type(error).__name__}: {error}"

    if failure is None:
        status = JobStatus.SUCCEEDED
    else:
        status = JobStatus.FAILED
        try:
            check_json(failure)
        except InvalidPayload:
            failure = "the job failed with an error that cannot be stored as text"
        # Read the log again first: if the failure was a step that did
        # not go in, the writer's idea of the next seq may be wrong.
        await _stored(writer.reread())
        await writer.append(StepKind.ERROR, {"message": failure})
    await _stored(
        held.leave_running(
            engine, job_id=job.id, lease_epoch=lease.epoch, new_status=status, error=failure
        )
    )
    log.info("job finished", extra={"job_id": job.id, "status": status.value, "error": failure})
    return status


async def run_job(
    engine: AsyncEngine,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    settings: AgentSettings,
    *,
    claimed_at: float | None = None,
) -> JobStatus | None:
    """Run a job this worker has claimed. Returns the status it left the job in.

    `claimed_at` is the monotonic time just before the claim was sent. Left
    out, the lease is counted from now.

    A failure of the job (a bad spec, a provider error, a response that
    cannot be used) is recorded as an `error` step and the status `failed`.

    Returns None when the worker dropped the job: its lease was gone, or the
    database could not be reached. Nothing is written then. The job stays
    as it is, and the workspace stays for the worker that holds the job next.
    The workspace is removed only by the worker that made the job final.
    """
    lease = Lease(
        job.id,
        job.lease_epoch,
        settings.lease,
        claimed_at=time.monotonic() if claimed_at is None else claimed_at,
    )
    heartbeat = asyncio.create_task(keep_renewing(engine, lease, settings.heartbeat))
    try:
        status = await _run(engine, provider, tools, job, settings, lease)
    except LeaseLost:
        log.warning(
            "job dropped: the lease is gone",
            extra={"job_id": job.id, "lease_epoch": job.lease_epoch},
        )
        return None
    except _StoreUnreachable as error:
        log.warning(
            "job dropped: the database could not be reached",
            extra={"job_id": job.id, "lease_epoch": job.lease_epoch, "error": str(error)},
        )
        return None
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    if status in FINAL_STATUSES:
        await asyncio.to_thread(remove_workspace, settings.workspace_root, job.id)
    return status
