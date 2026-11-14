"""The agent loop: run one claimed job until this worker is done with it.

Each pass reads the step log back as a conversation (replay), does the one
thing that comes next, and writes it down as a step: a model call, or the
tool calls the model asked for.

The worker holds the job through a lease. A heartbeat renews it while the
job runs, and every write is fenced by the lease epoch. A worker that finds
its lease gone, or cannot reach the database, drops the job: it writes
nothing more and leaves the status and the workspace to whoever holds the
job next.

The token budget is kept with an upper bound of each call's input
(`runtime.worker.budget`). Not built yet: the deadline is not enforced.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.failpoints import Failpoints
from runtime.provider.base import ModelRequest, ModelResponse, Provider, ProviderError
from runtime.spec import JobSpec
from runtime.store import held
from runtime.store.held import LeaseLost
from runtime.store.jobs import Job
from runtime.store.payload import InvalidPayload, check_json
from runtime.store.status import FINAL_STATUSES, JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import AnyTool, ToolCall, ToolRegistry, ToolResult, operation_id
from runtime.tools.sandboxed import SandboxTool
from runtime.worker.budget import input_bound, max_tokens_within
from runtime.worker.lease import Lease, keep_renewing
from runtime.worker.recovery import Ask, Recovered, decide
from runtime.worker.replay import Replay, replay
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
    # Crash points for chaos tests. None is switched on unless FAILPOINT is set.
    failpoints: Failpoints = field(default_factory=Failpoints)


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

    def __init__(self, engine: AsyncEngine, lease: Lease, last_seq: int) -> None:
        self._engine = engine
        self._lease = lease
        self.last_seq = last_seq
        # Tokens this worker added to the job's count.
        self.tokens_added = 0
        # Set once the conversation is known. Each step written is folded
        # into it, so the log is replayed once and not after every step.
        self.state: Replay | None = None

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        seq = self.last_seq + 1
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
        self.last_seq = seq
        self.tokens_added += tokens
        if self.state is not None:
            self.state.apply(step)
        log.info("step", extra={"job_id": self._lease.job_id, "seq": seq, "kind": kind.value})
        return step


async def _read_log(engine: AsyncEngine, job_id: UUID) -> list[Step]:
    async with engine.connect() as conn:
        return await read_steps(conn, job_id)


def _usable(result: object) -> ToolResult:
    """The tool's result if it can be stored and sent to the model, else an error result."""
    if not isinstance(result, ToolResult) or not isinstance(result.content, str):
        return ToolResult("the tool returned no text result", is_error=True)
    try:
        check_json(result.content)
    except InvalidPayload as error:
        return ToolResult(f"the tool's output cannot be stored: {error}", is_error=True)
    return result


async def _run_tool(tool: AnyTool | None, name: str, call: ToolCall) -> ToolResult:
    """Run one tool call. A failure of the tool becomes an error result for the model.

    A sandboxed tool runs its command in a container and is awaited here. A
    tool that is runtime code runs in a thread, so it may block.
    """
    if tool is None:
        return ToolResult(f"unknown tool: {name}", is_error=True)
    try:
        if isinstance(tool, SandboxTool):
            result = await tool.run(call)
        else:
            result = await asyncio.to_thread(tool.run, call)
    # SystemExit too: a tool that calls sys.exit() must not take the worker down.
    except (Exception, SystemExit) as error:
        log.exception("tool raised", extra={"tool": name})
        result = ToolResult(f"the tool failed: {error}", is_error=True)
    return _usable(result)


async def _next_tool_call(
    writer: _StepWriter,
    tools: ToolRegistry,
    job: Job,
    workspace: Path,
    lease: Lease,
    state: Replay,
    failpoints: Failpoints,
    stop: asyncio.Event,
) -> bool:
    """Deal with the first open tool call. False means the job now waits for a person.

    Normally that is: write the intent, run the tool, write the result. A
    call whose intent is already in the log was interrupted when a worker
    stopped. It is rerun, answered from the tool's check, or put before a
    person (see `runtime.worker.recovery`).
    """
    block = state.pending[0]
    tool_use_id, name = block["id"], block["name"]
    tool = tools.get(name)
    call = ToolCall(
        workspace=workspace, input=block["input"], operation_id=operation_id(job.id, tool_use_id)
    )

    interrupted = state.interrupted
    if interrupted is not None and interrupted.get("tool_use_id") == tool_use_id:
        lease.check()
        decision = await asyncio.to_thread(decide, tool, call, approved=state.approval == "given")
        log.info(
            "interrupted tool call",
            extra={
                "job_id": job.id,
                "tool": name,
                "operation_id": call.operation_id,
                "decision": type(decision).__name__.lower(),
            },
        )
        if isinstance(decision, Recovered):
            await writer.append(
                StepKind.TOOL_RESULT,
                {
                    "tool_use_id": tool_use_id,
                    "content": decision.result.content,
                    "is_error": decision.result.is_error,
                    # The result came from the tool's check. The tool did not run again.
                    "recovered": True,
                },
            )
            return True
        if isinstance(decision, Ask):
            # A worker may have stopped between asking and parking the job.
            # The question is then already in the log.
            if state.approval != "requested":
                await writer.append(
                    StepKind.APPROVAL_REQUESTED,
                    {
                        "tool_use_id": tool_use_id,
                        "name": name,
                        "input": block["input"],
                        "operation_id": call.operation_id,
                        "reason": decision.reason,
                    },
                )
            return False
        if stop.is_set():
            # Asking the check took time and the worker was told to stop
            # meanwhile. The rerun has no intent yet, so it does not start.
            return True

    # Before the intent: a call that will not start must not leave an
    # intent behind, which would look interrupted. Once the intent is
    # written the worker is committed to the run, also if it is told to
    # stop while writing it: the tool runs and its result is stored.
    lease.check()
    await writer.append(
        StepKind.TOOL_INTENT,
        {"tool_use_id": tool_use_id, "name": name, "input": block["input"]},
    )
    failpoints.hit("after_tool_intent", takeovers=job.takeovers, tool=name)
    # And immediately before the run, in case the write was slow.
    lease.check()
    result = await _run_tool(tool, name, call)
    failpoints.hit("after_tool_ran", takeovers=job.takeovers, tool=name)
    payload: dict[str, Any] = {
        "tool_use_id": tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
    }
    if result.truncated:
        payload["truncated"] = True
    await writer.append(StepKind.TOOL_RESULT, payload)
    failpoints.hit("after_tool_result", takeovers=job.takeovers, tool=name)
    return True


def _response_problem(response: ModelResponse, used_ids: set[str]) -> str | None:
    """What is wrong with a response's shape, or None. Checked before any tool runs.

    `used_ids` are the ids of the tool calls of earlier responses in the job.
    """
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
    # A call's operation id is the job's id and the call's id. A second call
    # under the same id would be taken for the first one when a tool's
    # check looks it up.
    if not used_ids.isdisjoint(ids):
        return "a tool_use block has the id of an earlier call"
    return None


async def _complete_unless_stopped(
    provider: Provider, request: ModelRequest, stop: asyncio.Event, lease: Lease
) -> ModelResponse | None:
    """Call the model. Returns None if `stop` was set before the answer was complete.

    The call is then cancelled at once. Its request step stays in the log
    without a response, and such a request is sent again by whoever runs
    the job next. A complete answer is never thrown away.

    The call is cancelled as well when the heartbeat learns that the lease
    is gone: its answer would be refused by the fenced write, so there is
    nothing to wait for. Raises LeaseLost then.
    """
    if stop.is_set():
        # The stop arrived while the request step was written. Nothing is sent.
        return None
    call = asyncio.ensure_future(provider.complete(request))
    stopped = asyncio.ensure_future(stop.wait())
    lost = asyncio.ensure_future(lease.wait_lost())
    try:
        await asyncio.wait({call, stopped, lost}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopped.cancel()
        lost.cancel()
        if not call.done():
            call.cancel()
        await asyncio.gather(call, stopped, lost, return_exceptions=True)
    for watcher in (stopped, lost):
        # A watcher that failed says nothing about a stop or the lease. Read
        # as a stop, the request would be sent again and again.
        if not watcher.cancelled() and (broken := watcher.exception()) is not None:
            raise broken
    if call.cancelled():
        if lease.lost:
            raise LeaseLost(lease.job_id, lease.epoch)
        return None
    return call.result()


async def _loop(
    writer: _StepWriter,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    spec: JobSpec,
    settings: AgentSettings,
    workspace: Path,
    lease: Lease,
    steps: list[Step],
    stop: asyncio.Event,
) -> JobStatus:
    """Run until the model ends its turn, a person has to decide, or the worker is stopped.

    Returns the status to leave the job in: `succeeded`, `needs_approval`,
    or `queued` when `stop` was set and the job is to be given back. Raises
    _JobFailed if the job cannot finish, and LeaseLost as soon as the worker
    can no longer rely on its lease.
    """
    model = spec.model or settings.default_model
    failpoints = settings.failpoints
    # The log is replayed once, here. This is also how a job is resumed: the
    # steps a stopped worker left behind are read like any others.
    state = replay(spec.prompt, steps)
    writer.state = state
    while True:
        if state.problem:
            raise _JobFailed(f"the model's response is not usable: {state.problem}")

        calls_open = state.stop_reason == "tool_use" and bool(state.pending)
        # A step boundary: everything started so far has its step. A worker
        # that was told to stop starts nothing more. A job with nothing left
        # to start is finished below instead of being handed back.
        if stop.is_set() and (state.needs_model_call or calls_open):
            return JobStatus.QUEUED

        if state.needs_model_call:
            definitions = tools.definitions()
            used = job.tokens_used + writer.tokens_added
            bound = input_bound(
                system=spec.system,
                tools=definitions,
                messages=state.messages,
                counted=state.counted,
                counted_messages=state.counted_messages,
            )
            max_tokens = max_tokens_within(job.token_budget, used, bound, settings.max_tokens)
            if max_tokens is None:
                # Before the request step: a call that is not made leaves none.
                raise _JobFailed(
                    f"the token budget does not cover the next model call: {used} of"
                    f" {job.token_budget} tokens used, and its input can take up to {bound}"
                )
            request = ModelRequest(
                model=model,
                max_tokens=max_tokens,
                messages=state.messages,
                system=spec.system,
                tools=definitions,
                user_id=str(job.id),
                mid_stream=(
                    (lambda: failpoints.hit("mid_stream", takeovers=job.takeovers))
                    if failpoints.name == "mid_stream"
                    else None
                ),
            )
            lease.check()
            await writer.append(
                StepKind.MODEL_REQUEST,
                {
                    "model": model,
                    "max_tokens": request.max_tokens,
                    "messages": len(state.messages),
                    # The upper bound of the input and the count before the
                    # call, so the log shows the budget was kept.
                    "input_bound": bound,
                    "tokens_used": used,
                },
            )
            failpoints.hit("after_model_request", takeovers=job.takeovers)
            # Immediately before the call: a worker that was paused past its
            # lease must not send it.
            lease.check()
            try:
                response = await _complete_unless_stopped(provider, request, stop, lease)
            except ProviderError as error:
                raise _JobFailed(f"model call failed: {error}") from error
            if response is None:
                continue
            payload: dict[str, Any] = {
                "id": response.id,
                "content": response.content,
                "stop_reason": response.stop_reason,
                "usage": {
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                },
            }
            problem = _response_problem(response, state.used_ids)
            try:
                check_json(payload)
            except InvalidPayload as error:
                # The tokens were spent, so the step and the count are still
                # written. Only the content that cannot be stored is left out.
                problem = problem or f"it cannot be stored: {error}"
                payload["content"] = []
                payload["content_dropped"] = True
            if problem:
                # Stored with the step, so a worker that takes the job over
                # here fails it for the same reason.
                payload["problem"] = problem
            await writer.append(
                StepKind.MODEL_RESPONSE,
                payload,
                tokens=response.input_tokens + response.output_tokens,
            )
            continue

        # A response cut off at max_tokens can hold half a tool call, so the
        # stop reason is checked before any tool runs. The calls of one
        # response run one after another, one per pass.
        if calls_open:
            if not await _next_tool_call(
                writer, tools, job, workspace, lease, state, settings.failpoints, stop
            ):
                return JobStatus.NEEDS_APPROVAL
            continue

        if state.stop_reason in _FINISHED:
            return JobStatus.SUCCEEDED
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
    stop: asyncio.Event,
) -> JobStatus:
    """Run the job and record its outcome. Returns the status it was left in:
    `succeeded`, `failed`, `needs_approval` when a person has to decide
    about an interrupted tool call, or `queued` when the worker was stopped
    and released the job.

    Raises LeaseLost or _StoreUnreachable when the worker has to drop the job.
    """
    settings.failpoints.hit("after_claim", takeovers=job.takeovers)
    steps = await _stored(_read_log(engine, job.id))
    writer = _StepWriter(engine, lease, last_seq=steps[-1].seq if steps else 0)
    status = JobStatus.FAILED
    failure: str | None = None
    try:
        spec = JobSpec.model_validate(job.spec)
        # A worker that was paused past its lease right after the claim must
        # not write to the workspace: the job may be finished and its
        # workspace removed by now.
        lease.check()
        # Made with the spec's files on the first claim only. A job that is
        # taken over finds its workspace as its tools left it.
        workspace = await asyncio.to_thread(
            prepare_workspace, settings.workspace_root, job.id, spec.files
        )
        status = await _loop(
            writer, provider, tools, job, spec, settings, workspace, lease, steps, stop
        )
    except _JobFailed as error:
        failure = str(error)
    except LeaseLost, _StoreUnreachable:
        raise
    except Exception as error:
        log.exception("job crashed", extra={"job_id": job.id})
        failure = f"{type(error).__name__}: {error}"

    if failure is not None:
        try:
            check_json(failure)
        except InvalidPayload:
            failure = "the job failed with an error that cannot be stored as text"
        # One statement for the `error` step and the status: a worker that
        # stops here leaves the job failed, or not failed at all. The step
        # takes the next free seq by itself, also when the failure was a
        # step that did not go in at the seq the writer expected.
        step = await _stored(
            held.fail(engine, job_id=job.id, lease_epoch=lease.epoch, error=failure)
        )
        log.info("step", extra={"job_id": job.id, "seq": step.seq, "kind": step.kind.value})
    else:
        await _stored(
            held.leave_running(engine, job_id=job.id, lease_epoch=lease.epoch, new_status=status)
        )
    log.info(
        "job finished" if status in FINAL_STATUSES else "job handed back",
        extra={"job_id": job.id, "status": status.value, "error": failure},
    )
    return status


async def run_job(
    engine: AsyncEngine,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    settings: AgentSettings,
    *,
    claimed_at: float | None = None,
    stop: asyncio.Event | None = None,
) -> JobStatus | None:
    """Run a job this worker has claimed. Returns the status it left the job in.

    `claimed_at` is the monotonic time just before the claim was sent. Left
    out, the lease is counted from now.

    `stop` tells the worker to stop, as SIGTERM does. The job is then
    released at the next step boundary: it goes back to `queued` and the
    next claim is not a takeover. A model call in flight is abandoned. A
    running tool is waited for, so that it does not leave an intent without
    a result.

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
        status = await _run(engine, provider, tools, job, settings, lease, stop or asyncio.Event())
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
        try:
            await asyncio.to_thread(remove_workspace, settings.workspace_root, job.id)
        except Exception:
            # The job is final either way. Only its directory is left behind.
            log.exception("workspace could not be removed", extra={"job_id": job.id})
    return status
