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
(`runtime.worker.budget`). A cancel and the deadline halt the job
(`runtime.worker.halt`). A tool call that needs approval waits for a person
before it runs (`runtime.worker.approval`).
"""

import asyncio
import dataclasses
import inspect
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.failpoints import Failpoints
from runtime.provider.base import ModelRequest, ModelResponse, Provider, ProviderError
from runtime.sandbox.docker import SandboxError
from runtime.sandbox.runner import LONGEST_BOUND, ends_within
from runtime.scope import ScopeRefused, Target, resolve
from runtime.spec import JobSpec
from runtime.store import held
from runtime.store.clock import seconds_until
from runtime.store.held import LeaseLost, Wait
from runtime.store.jobs import DEADLINE_ERROR, Job
from runtime.store.payload import InvalidPayload, check_json
from runtime.store.status import FINAL_STATUSES, JobStatus
from runtime.store.steps import StatusCause, Step, StepKind, read_steps
from runtime.tools.base import AnyTool, Tool, ToolCall, ToolRegistry, ToolResult, operation_id
from runtime.tools.sandboxed import SandboxTool
from runtime.worker import backoff
from runtime.worker.approval import DENIED, DENIED_RERUN, gate_reason, not_offered
from runtime.worker.budget import input_bound, max_tokens_within
from runtime.worker.halt import Halt, Halted, HaltReason, watch_cancel
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
    # The random part of a wait for a busy provider. Tests pass a seeded one.
    rng: random.Random = field(default_factory=random.Random)


class _JobFailed(Exception):
    """The job cannot go on. The message becomes the job's error."""


class _StoreUnreachable(Exception):
    """A read or write of the job could not be sent to the database."""


class _ProviderWait(Exception):
    """The job waits for the provider, then sends its request again."""

    cause: StatusCause
    # Stored when the provider's error text cannot be.
    instead: str

    def __init__(self, error: ProviderError, attempt: int) -> None:
        super().__init__(str(error))
        self.error = error
        # Waits since the last response, of either cause, this one included.
        self.attempt = attempt


class _ProviderBusy(_ProviderWait):
    """The provider is rate limited or overloaded (ADR 0018)."""

    cause = StatusCause.PROVIDER_BUSY
    instead = "the provider was busy"


class _ProviderFault(_ProviderWait):
    """The provider failed in a way that may pass (ADR 0030)."""

    cause = StatusCause.PROVIDER_ERROR
    instead = "the provider failed"


# The longest provider error text a wait keeps in its step. The step log is
# append-only and a job can wait thousands of times; a longer text is cut.
WAIT_ERROR_LENGTH = 1000
_CUT = " [cut]"


def _wait(waiting: _ProviderWait, rng: random.Random) -> Wait:
    """The wait for the provider, with its error text cut to length."""
    text = str(waiting.error)
    if len(text) > WAIT_ERROR_LENGTH:
        text = text[: WAIT_ERROR_LENGTH - len(_CUT)] + _CUT
    return Wait(
        seconds=backoff.delay(waiting.attempt, waiting.error.retry_after, rng),
        attempt=waiting.attempt,
        retry_after=waiting.error.retry_after,
        error=_storable(text, waiting.instead),
        cause=waiting.cause,
    )


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
        self.engine = engine
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
                self.engine,
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


async def _race[T](
    work: Awaitable[T], *, lease: Lease, halt: Halt, stop: asyncio.Event | None = None
) -> tuple[bool, T | None]:
    """Await `work` unless the job is halted, the lease is lost, or `stop` is set first.

    Returns (True, its result) when `work` completed, also if something
    else happened at the same moment: a complete answer is never thrown
    away. Returns (False, None) when `stop` came first. Raises LeaseLost or
    Halted when one of those came first. Unless it completed, `work` is
    cancelled and waited for: a sandboxed call removes its containers then.
    """
    if lease.lost or halt.reason is not None:
        # Nothing is started for a job that is already halted or dropped.
        if inspect.iscoroutine(work):
            work.close()
        if lease.lost:
            raise LeaseLost(lease.job_id, lease.epoch)
        halt.check()
    task = asyncio.ensure_future(work)
    watchers: list[asyncio.Future[Any]] = [
        asyncio.ensure_future(lease.wait_lost()),
        asyncio.ensure_future(halt.wait()),
    ]
    if stop is not None:
        watchers.append(asyncio.ensure_future(stop.wait()))
    try:
        await asyncio.wait({task, *watchers}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for watcher in watchers:
            watcher.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *watchers, return_exceptions=True)
    for watcher in watchers:
        # A watcher that failed says nothing about a stop, the lease or a
        # halt. Read as one of them, the work would be given up for nothing.
        if not watcher.cancelled() and (broken := watcher.exception()) is not None:
            raise broken
    if task.done() and not task.cancelled():
        return True, task.result()
    if lease.lost:
        raise LeaseLost(lease.job_id, lease.epoch)
    halt.check()
    return False, None


async def _run_tool(
    tool: AnyTool | None, name: str, call: ToolCall, lease: Lease, halt: Halt
) -> ToolResult:
    """Run one tool call. A failure of the tool becomes an error result for the model.

    A sandboxed tool runs its command in a container. A tool that is runtime
    code runs in a thread, so it may block. When the job is halted or the
    lease is lost, the call is given up: a sandboxed command is killed and
    its containers removed, a thread runs on and its result is dropped.
    Raises Halted or LeaseLost then.
    """
    if tool is None:
        return ToolResult(f"unknown tool: {name}", is_error=True)

    async def attempt() -> ToolResult:
        # A failure of the tool is caught here, inside the task: asyncio lets
        # a SystemExit out of a task straight through the event loop.
        try:
            if isinstance(tool, SandboxTool):
                return await tool.run(call)
            return await asyncio.to_thread(tool.run, call)
        # SystemExit too: a tool that calls sys.exit() must not take the worker down.
        except (Exception, SystemExit) as error:
            log.exception("tool raised", extra={"tool": name})
            return ToolResult(f"the tool failed: {error}", is_error=True)

    _, result = await _race(attempt(), lease=lease, halt=halt)
    return _usable(result)


async def _wait_unless_stopped(
    seconds: float, stop: asyncio.Event, lease: Lease, halt: Halt
) -> bool:
    """Wait `seconds`. False if `stop` was set first. Raises LeaseLost or Halted."""
    done, _ = await _race(asyncio.sleep(seconds), lease=lease, halt=halt, stop=stop)
    return done


async def _wait_out(
    left_now: Callable[[], Awaitable[float]],
    stop: asyncio.Event,
    lease: Lease,
    halt: Halt,
    job_id: UUID,
) -> bool:
    """Wait until `left_now` says no time is left. False if `stop` was set first.

    The wait runs on the worker's own clock and `left_now` reads the clock
    that counts, which can run faster meanwhile, so it is asked again after
    each wait. Each ask is raced too: a database that does not answer does
    not hide a halt.
    """
    waited = False
    while True:
        asked, left = await _race(left_now(), lease=lease, halt=halt, stop=stop)
        if not asked:
            return False
        assert left is not None  # noqa: S101 - _race returns the result when it completed
        if left <= 0:
            return True
        if not waited:
            log.info(
                "waiting for an interrupted call to end",
                extra={"job_id": job_id, "seconds": round(left, 1)},
            )
            waited = True
        if not await _wait_unless_stopped(left, stop, lease, halt):
            return False


def _bound(tool: AnyTool | None, intent: dict[str, Any]) -> float | None:
    """Seconds after its intent by which an interrupted call has ended.

    For a sandboxed call that is its container. For a tool in the worker it
    is the bound the tool keeps to itself (ADR 0025). None when the call has
    neither. The intent's own number is used, so a worker with other
    settings waits as long as the one that started the call. A number above
    what any settings give is cut to that.
    """
    recorded = intent.get("ends_within")
    if (
        isinstance(recorded, int | float)
        and not isinstance(recorded, bool)
        and math.isfinite(recorded)
        and recorded >= 0
    ):
        return min(float(recorded), LONGEST_BOUND)
    if isinstance(tool, SandboxTool):
        return ends_within(tool.sandbox.settings)
    if isinstance(tool, Tool):
        return tool.ends_within
    return None


async def _outlast_container(
    writer: _StepWriter,
    tool: AnyTool | None,
    job: Job,
    lease: Lease,
    intent: dict[str, Any],
    written: datetime | None,
    stop: asyncio.Event,
    halt: Halt,
) -> bool:
    """Make sure the interrupted call has ended. False if `stop` came first.

    The container may still run: the worker that started it stopped, the
    container did not (ADR 0008). Containers of earlier lease epochs on this
    host are removed. One on another host, or one whose start request was
    still on its way, cannot be, so the worker also waits until the call's
    bound has passed since its intent, by the database clock. That clock
    is read again after the wait: the decision is never before the bound.

    A call of a tool in the worker has no container, and may still be
    sending: its thread outlives a lost lease. Only the wait applies to it
    (ADR 0025).
    """
    bound = _bound(tool, intent)
    if bound is None or written is None:
        return True
    lease.check()
    if isinstance(tool, SandboxTool):
        try:
            removed = await tool.sandbox.remove_older(job.id, lease.epoch)
        except SandboxError:
            # The wait below still covers the container.
            log.exception("containers of an earlier lease not removed", extra={"job_id": job.id})
        else:
            log.info(
                "containers of an earlier lease removed",
                extra={"job_id": job.id, "removed": removed},
            )
    ends = written + timedelta(seconds=bound)

    async def left_now() -> float:
        return await _stored(seconds_until(writer.engine, ends))

    return await _wait_out(left_now, stop, lease, halt, job.id)


async def _next_tool_call(
    writer: _StepWriter,
    tools: ToolRegistry,
    job: Job,
    workspace: Path,
    lease: Lease,
    state: Replay,
    failpoints: Failpoints,
    stop: asyncio.Event,
    scope: tuple[Target, ...] | None,
    halt: Halt,
    gated: tuple[str, ...] = (),
) -> held.Question | None:
    """Deal with the first open tool call. Returns the question the job now
    waits on when a person has to decide, None when the call was dealt with.

    Normally that is: write the intent, run the tool, write the result. A
    call whose intent is already in the log was interrupted when a worker
    stopped. It is rerun, answered from the tool's check, or put before a
    person (see `runtime.worker.recovery`).

    A call of a tool that needs approval, by its definition or by the names
    in `gated` from the job's spec, is put before a person first (ADR 0011):
    the question is written and the job parked, with no intent. It gets its
    intent and runs once the log holds an approval for it. A call a person
    denied gets an error result and never runs.
    """
    block = state.pending[0]
    tool_use_id, name = block["id"], block["name"]
    tool = tools.get(name)
    call = ToolCall(
        workspace=workspace,
        input=block["input"],
        operation_id=operation_id(job.id, tool_use_id),
        lease_epoch=lease.epoch,
    )
    # A person's answer counts for the call it names and for no other.
    answer = state.approval if state.approval_for == tool_use_id else None
    interrupted = state.interrupted
    resumed = interrupted is not None and interrupted.get("tool_use_id") == tool_use_id
    why_gated = gate_reason(tool, name, gated)
    # The log binds a worker whose own tools would not have asked, for
    # example during a deploy that changes a tool's definition: a question
    # that came before any intent was the gate's, and so was an intent
    # that says it ran under an approval.
    if interrupted is not None and resumed:
        needs_person = why_gated is not None or interrupted.get("needs_approval") is True
    else:
        needs_person = why_gated is not None or answer is not None

    async def ask(reason: str, why: held.QuestionReason) -> held.Question:
        # A worker may have stopped between asking and parking the job. The
        # question is then already in the log, and the job is parked on it.
        if answer == "requested":
            if state.approval_question is None:
                # Replay sets both together. Not an assert: those go with -O.
                raise RuntimeError("the log has a question without its seq")
            return held.Question(state.approval_question, why)
        step = await writer.append(
            StepKind.APPROVAL_REQUESTED,
            {
                "tool_use_id": tool_use_id,
                "name": name,
                "input": block["input"],
                "operation_id": call.operation_id,
                "reason": reason,
            },
        )
        return held.Question(step.seq, why)

    async def refuse(content: str) -> None:
        await writer.append(
            StepKind.TOOL_RESULT,
            {"tool_use_id": tool_use_id, "content": content, "is_error": True, "denied": True},
        )

    if interrupted is not None and resumed:
        # Nothing is decided while the interrupted call may still be running.
        if not await _outlast_container(
            writer, tool, job, lease, interrupted, state.interrupted_at, stop, halt
        ):
            return None
        lease.check()
        if answer == "denied":
            # A person refused the rerun. Whether the interrupted run
            # happened stays unknown, and the result says so.
            await refuse(DENIED_RERUN)
            return None
        # Raced: a halt does not wait for a slow check. The check runs on in
        # its thread; it only asks, it changes nothing.
        _, found = await _race(
            asyncio.to_thread(decide, tool, call, approved=answer == "given", gated=needs_person),
            lease=lease,
            halt=halt,
        )
        assert found is not None  # noqa: S101 - _race returns the result when it completed
        decision = found
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
            return None
        if isinstance(decision, Ask):
            return await ask(decision.reason, held.QuestionReason.INTERRUPTED)
        if stop.is_set():
            # Asking the check took time and the worker was told to stop
            # meanwhile. The rerun has no intent yet, so it does not start.
            return None
    elif answer == "denied":
        # Checked before the gate: a denial holds also on a worker whose
        # tools would not have asked.
        lease.check()
        await refuse(DENIED)
        return None
    elif needs_person and answer != "given":
        # The gate. No intent is written, so nothing of the call has
        # happened and nothing looks interrupted. Without a reason of this
        # worker's own, the question is already in the log.
        lease.check()
        return await ask(
            why_gated or "a person was asked about this call before it ran",
            held.QuestionReason.BEFORE_RUN,
        )

    # Before the intent: a call that will not start must not leave an
    # intent behind, which would look interrupted. Once the intent is
    # written the worker is committed to the run, also if it is told to
    # stop while writing it: the tool runs and its result is stored.
    lease.check()
    intent: dict[str, Any] = {"tool_use_id": tool_use_id, "name": name, "input": block["input"]}
    if needs_person:
        # This run has an approval. If it is interrupted, whoever takes the
        # job over asks again, whatever its own tools say.
        intent["needs_approval"] = True
    if isinstance(tool, Tool) and tool.ends_within is not None:
        # A worker that takes the job over waits this long after the intent
        # before it asks the tool's check: the call's request may still be
        # on its way then (ADR 0025).
        intent["ends_within"] = tool.ends_within
    if isinstance(tool, SandboxTool):
        # A worker that takes the job over waits this long after the intent
        # before it decides about the call.
        intent["ends_within"] = ends_within(tool.sandbox.settings)
        if tool.networked and scope is not None:
            # Names are resolved once, here, and the call gets these
            # addresses and no others (ADR 0009). They are in the log.
            try:
                if tool.sandbox.settings.guard_image is None:
                    raise ScopeRefused("this worker has no guard image, so it cannot enforce one")
                done, looked_up = await _race(
                    resolve(scope, tool.sandbox.settings.deny), lease=lease, halt=halt, stop=stop
                )
                if not done:
                    # Told to stop during the lookup. Nothing of the call has
                    # happened, so the job is handed back without it.
                    return None
                assert looked_up is not None  # noqa: S101 - _race returns the result
                resolved = looked_up
            except ScopeRefused as refused:
                # Before the intent: the call does not start.
                await writer.append(
                    StepKind.TOOL_RESULT,
                    {
                        "tool_use_id": tool_use_id,
                        "content": f"the job's scope cannot be used for this call: {refused}",
                        "is_error": True,
                    },
                )
                return None
            call = dataclasses.replace(call, scope=resolved)
            intent["scope"] = resolved.record()
    # The last moment to give up a call: after the intent the tool runs. A
    # worker told to stop starts no call; the job goes back with it open.
    if stop.is_set():
        return None
    halt.check()
    # Read before the write: a worker that takes the job over counts from
    # the intent, and the write itself can be slow.
    call = dataclasses.replace(call, started=time.monotonic())
    await writer.append(StepKind.TOOL_INTENT, intent)
    failpoints.hit("after_tool_intent", takeovers=job.takeovers, tool=name)
    # And immediately before the run, in case the write was slow.
    lease.check()
    result = await _run_tool(tool, name, call, lease, halt)
    failpoints.hit("after_tool_ran", takeovers=job.takeovers, tool=name)
    payload: dict[str, Any] = {
        "tool_use_id": tool_use_id,
        "content": result.content,
        "is_error": result.is_error,
    }
    if result.truncated:
        payload["truncated"] = True
    if result.dropped_packets is not None:
        payload["dropped_packets"] = result.dropped_packets
    await writer.append(StepKind.TOOL_RESULT, payload)
    failpoints.hit("after_tool_result", takeovers=job.takeovers, tool=name)
    return None


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
    provider: Provider, request: ModelRequest, stop: asyncio.Event, lease: Lease, halt: Halt
) -> ModelResponse | None:
    """Call the model. Returns None if `stop` was set before the answer was complete.

    The call is then cancelled at once. Its request step stays in the log
    without a response, and such a request is sent again by whoever runs
    the job next. A complete answer is never thrown away.

    The call is cancelled as well when the heartbeat learns that the lease
    is gone (raises LeaseLost: its answer would be refused by the fenced
    write), and when the job is halted (raises Halted).
    """
    if stop.is_set():
        # The stop arrived while the request step was written. Nothing is sent.
        return None
    done, response = await _race(provider.complete(request), lease=lease, halt=halt, stop=stop)
    return response if done else None


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
    halt: Halt,
) -> tuple[JobStatus, held.Question | None]:
    """Run until the model ends its turn, a person has to decide, or the worker is stopped.

    Returns the status to leave the job in: `succeeded`, `needs_approval`,
    or `queued` when `stop` was set and the job is to be given back. With
    `needs_approval` comes the question the job waits on, else None. Raises
    _JobFailed if the job cannot finish, _ProviderWait when it has to wait,
    LeaseLost as soon as the worker can no longer rely on its lease, and
    Halted when the job is halted.
    """
    model = spec.model or settings.default_model
    failpoints = settings.failpoints
    gated = spec.needs_approval or ()
    if unknown := not_offered(gated, tools):
        # Before anything runs. Such a name gates nothing: if it is a
        # misspelt one, the tool it was meant for would run without a question.
        more = f" and {len(unknown) - 5} more" if len(unknown) > 5 else ""
        raise _JobFailed(
            "the spec asks for approval before tools this worker does not offer: "
            + ", ".join(repr(name) for name in unknown[:5])
            + more
        )
    # The log is replayed once, here. This is also how a job is resumed: the
    # steps a stopped worker left behind are read like any others.
    state = replay(spec.prompt, steps)
    writer.state = state
    while True:
        # A step boundary: a halted job starts nothing more.
        halt.check()
        if state.problem:
            raise _JobFailed(f"the model's response is not usable: {state.problem}")

        calls_open = state.stop_reason == "tool_use" and bool(state.pending)
        # A step boundary: everything started so far has its step. A worker
        # that was told to stop starts nothing more. A job with nothing left
        # to start is finished below instead of being handed back.
        if stop.is_set() and (state.needs_model_call or calls_open):
            return JobStatus.QUEUED, None

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
                response = await _complete_unless_stopped(provider, request, stop, lease, halt)
            except ProviderError as error:
                if error.busy:
                    # The request step stays without a response. It is
                    # sent again once the wait is over.
                    raise _ProviderBusy(error, state.waits_in_a_row + 1) from error
                if error.retryable and not error.busy:
                    # The same wait, up to a cap (ADR 0030). The counts come
                    # from the log, so any worker continues them.
                    cap = backoff.error_cap(state.errors_in_a_row, state.errors_in_job)
                    if cap is not None:
                        raise _JobFailed(f"model call failed: {cap}: {error}") from error
                    raise _ProviderFault(error, state.waits_in_a_row + 1) from error
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
            asked = await _next_tool_call(
                writer,
                tools,
                job,
                workspace,
                lease,
                state,
                settings.failpoints,
                stop,
                spec.scope,
                halt,
                gated,
            )
            if asked is not None:
                return JobStatus.NEEDS_APPROVAL, asked
            continue

        if state.stop_reason in _FINISHED:
            return JobStatus.SUCCEEDED, None
        failure = _FAILURES.get(
            state.stop_reason or "", f"unexpected stop_reason: {state.stop_reason}"
        )
        lowered = state.requested_max_tokens
        if (
            state.stop_reason == "max_tokens"
            and lowered is not None
            and lowered < settings.max_tokens
        ):
            failure += f", which the token budget had lowered to {lowered}"
        raise _JobFailed(failure)


def _storable(text: str, instead: str) -> str:
    """`text`, or `instead` if a JSON column cannot hold it."""
    try:
        check_json(text)
    except InvalidPayload:
        return instead
    return text


async def _run(
    engine: AsyncEngine,
    provider: Provider,
    tools: ToolRegistry,
    job: Job,
    settings: AgentSettings,
    lease: Lease,
    stop: asyncio.Event,
    halt: Halt,
) -> JobStatus:
    """Run the job and record its outcome. Returns the status it was left in:
    `succeeded`, `failed`, `cancelled`, `needs_approval` when a person has
    to decide about a tool call, `waiting` when the provider has to be waited for, or
    `queued` when the worker was stopped and released the job.

    The job is halted at its deadline, by the database clock: the time left
    is read once, here, and a timer kept on the worker's clock.

    Raises LeaseLost or _StoreUnreachable when the worker has to drop the job.
    """
    settings.failpoints.hit("after_claim", takeovers=job.takeovers)
    steps = await _stored(_read_log(engine, job.id))
    writer = _StepWriter(engine, lease, last_seq=steps[-1].seq if steps else 0)
    status = JobStatus.FAILED
    failure: str | None = None
    wait: Wait | None = None
    # The question a parked job waits on.
    question: held.Question | None = None
    left = await _stored(seconds_until(engine, job.deadline))
    if left <= 0:
        halt.set(HaltReason.DEADLINE)
    deadline = asyncio.get_running_loop().call_later(max(left, 0), halt.set, HaltReason.DEADLINE)
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
        status, question = await _loop(
            writer, provider, tools, job, spec, settings, workspace, lease, steps, stop, halt
        )
    except Halted as halted:
        if halted.reason is HaltReason.CANCELLED:
            status = JobStatus.CANCELLED
        else:
            failure = DEADLINE_ERROR
    except _JobFailed as error:
        failure = str(error)
    except _ProviderWait as waiting:
        status = JobStatus.WAITING
        wait = _wait(waiting, settings.rng)
    except LeaseLost, _StoreUnreachable:
        raise
    except Exception as error:
        log.exception("job crashed", extra={"job_id": job.id})
        failure = f"{type(error).__name__}: {error}"
    finally:
        deadline.cancel()

    if failure is not None:
        failure = _storable(failure, "the job failed with an error that cannot be stored as text")
        # One statement for the `error` step and the status: a worker that
        # stops here leaves the job failed, or not failed at all. The step
        # takes the next free seq by itself, also when the failure was a
        # step that did not go in at the seq the writer expected.
        step = await _stored(
            held.fail(engine, job_id=job.id, lease_epoch=lease.epoch, error=failure)
        )
        log.info("step", extra={"job_id": job.id, "seq": step.seq, "kind": step.kind.value})
    else:
        left_as = await _stored(
            held.leave_running(
                engine,
                job_id=job.id,
                lease_epoch=lease.epoch,
                new_status=status,
                wait=wait,
                question=question,
            )
        )
        # A job that would wait for a person or the provider is
        # cancelled instead if its cancel was requested meanwhile.
        status = left_as.status
        if status is JobStatus.WAITING:
            log.info(
                "job waits for the provider",
                extra={
                    "job_id": job.id,
                    "not_before": left_as.not_before,
                    "attempt": wait.attempt if wait else None,
                    "cause": wait.cause.value if wait else None,
                },
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

    A requested cancel, seen within a second, and the deadline halt the job
    (ADR 0010): a model call in flight is abandoned, a running sandboxed
    command is killed, and the job ends `cancelled` or `failed`.

    A failure of the job (a bad spec, a provider error that will not pass or
    is over its cap, a response that cannot be used) is recorded as an
    `error` step and the status `failed`.

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
    halt = Halt(job.id)
    watcher = asyncio.create_task(watch_cancel(engine, job.id, halt))
    try:
        status = await _run(
            engine, provider, tools, job, settings, lease, stop or asyncio.Event(), halt
        )
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
        watcher.cancel()
        await asyncio.gather(heartbeat, watcher, return_exceptions=True)
    if status in FINAL_STATUSES:
        try:
            await asyncio.to_thread(remove_workspace, settings.workspace_root, job.id)
        except Exception:
            # The job is final either way. Only its directory is left behind.
            log.exception("workspace could not be removed", extra={"job_id": job.id})
    return status
