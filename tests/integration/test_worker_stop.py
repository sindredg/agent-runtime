"""A worker that is told to stop gives its job back instead of letting the lease run out.

`stop` is the event the worker's SIGTERM handler sets. Real Postgres; the
provider and the tool are the recording stand-ins of the resume tests.
"""

import asyncio
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import CheckAnswer, CheckOutcome, Tool, ToolCall, ToolRegistry
from runtime.worker.agent import run_job
from runtime.worker.loop import work

from .conftest import Stack, expire_lease
from .test_resume import SPEC, Acts, Script, op, uninterrupted

K = StepKind
S = JobStatus


class Cancellable(Script):
    """The resume tests' provider, which also records a call that was cancelled."""

    def __init__(self, hang_at: int | None = None) -> None:
        super().__init__(hang_at)
        self.cancelled = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            return await super().complete(request)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


async def read(engine: AsyncEngine, job: Job) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        found = await get_job(conn, job.id)
        assert found is not None
        return found, await read_steps(conn, job.id)


async def finish_with_a_second_worker(
    engine: AsyncEngine, stack: Stack, script: Script, acts: Acts
) -> tuple[Job | None, JobStatus | None]:
    """Claim at once, without waiting for any lease to run out, and run the job."""
    async with engine.begin() as conn:
        claim = await claim_job(conn, worker_id="w2")
    if claim is None:
        return None, None
    return claim, await run_job(engine, script, acts.registry(), claim, stack.settings())


def test_a_stop_during_a_model_call_abandons_the_call_and_releases_the_job(stack: Stack) -> None:
    script = Cancellable(hang_at=1)
    acts = Acts(idempotent=True)

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> dict[str, Any]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        stop = asyncio.Event()
        first = asyncio.create_task(
            run_job(engine, script, acts.registry(), first_claim, stack.settings(), stop=stop)
        )
        await script.hanging.wait()
        stop.set()
        async with asyncio.timeout(10):
            first_status = await first
        released, steps_at_release = await read(engine, first_claim)
        script.hang_at = None
        second_claim, second_status = await finish_with_a_second_worker(engine, stack, script, acts)
        final, steps = await read(engine, first_claim)
        return {
            "first_status": first_status,
            "released": released,
            "steps_at_release": steps_at_release,
            "second_claim": second_claim,
            "second_status": second_status,
            "final": final,
            "steps": steps,
        }

    seen = stack.run(body)
    assert seen["first_status"] is S.QUEUED
    released: Job = seen["released"]
    assert (released.status, released.lease_owner, released.lease_expires_at) == (
        S.QUEUED,
        None,
        None,
    )
    assert (released.lease_epoch, released.takeovers, released.error) == (1, 0, None)
    # The call in flight was dropped at once: there is a request and no response.
    assert script.cancelled == 1
    assert [step.kind for step in seen["steps_at_release"]] == [K.MODEL_REQUEST]
    # The next claim is an ordinary one: no lease had to run out, no takeover is counted.
    second_claim: Job = seen["second_claim"]
    assert (second_claim.lease_epoch, second_claim.takeovers) == (2, 0)
    assert seen["second_status"] is S.SUCCEEDED
    assert seen["final"].takeovers == 0
    assert script.requests[-1] == uninterrupted(stack)


def test_a_stop_during_a_tool_run_waits_for_the_tool_and_stores_its_result(stack: Stack) -> None:
    script = Cancellable()
    acts = Acts(hang_at=1)

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> dict[str, Any]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        stop = asyncio.Event()
        first = asyncio.create_task(
            run_job(engine, script, acts.registry(), first_claim, stack.settings(), stop=stop)
        )
        await asyncio.to_thread(acts.hanging.wait, 30)
        stop.set()
        await asyncio.sleep(0.5)
        still_running = not first.done()
        steps_while_waiting = (await read(engine, first_claim))[1]
        acts.hang_at = None
        acts.release.set()
        async with asyncio.timeout(10):
            first_status = await first
        released, steps_at_release = await read(engine, first_claim)
        second_claim, second_status = await finish_with_a_second_worker(engine, stack, script, acts)
        final, steps = await read(engine, first_claim)
        return {
            "still_running": still_running,
            "steps_while_waiting": steps_while_waiting,
            "first_status": first_status,
            "released": released,
            "steps_at_release": steps_at_release,
            "second_claim": second_claim,
            "second_status": second_status,
            "final": final,
            "steps": steps,
        }

    seen = stack.run(body)
    # The tool was not abandoned: the worker waited for it and stored the result.
    assert seen["still_running"]
    assert [step.kind for step in seen["steps_while_waiting"]] == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
    ]
    assert seen["first_status"] is S.QUEUED
    assert [step.kind for step in seen["steps_at_release"]] == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
    ]
    assert seen["released"].status is S.QUEUED
    # So there is no interrupted call. The tool is not idempotent and has no
    # check: an intent without a result would have stopped the job for a person.
    assert seen["second_status"] is S.SUCCEEDED
    assert (seen["second_claim"].lease_epoch, seen["final"].takeovers) == (2, 0)
    job: Job = seen["final"]
    # The second call of the same response had not started. It ran once, afterwards.
    assert acts.runs == [(op(job, "toolu_a"), "a"), (op(job, "toolu_b"), "b")]
    assert [(step.kind, step.lease_epoch) for step in seen["steps"]][4:] == [
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.MODEL_REQUEST, 2),
        (K.MODEL_RESPONSE, 2),
    ]
    assert script.cancelled == 0


def test_a_worker_that_is_stopped_claims_no_further_job(stack: Stack) -> None:
    script = Cancellable(hang_at=1)
    acts = Acts(idempotent=True)

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> list[Job | None]:
        async with engine.begin() as conn:
            first = await stack.create(conn, SPEC)
        async with engine.begin() as conn:
            second = await stack.create(conn, SPEC)
        stop = asyncio.Event()
        worker = asyncio.create_task(
            work(
                engine,
                script,
                acts.registry(),
                stack.settings(),
                worker_id="w1",
                poll_interval=0.05,
                stop=stop,
            )
        )
        await script.hanging.wait()
        stop.set()
        async with asyncio.timeout(10):
            await worker
        async with engine.begin() as conn:
            return [await get_job(conn, first.id), await get_job(conn, second.id)]

    first, second = stack.run(body)
    assert first is not None
    assert second is not None
    # The job in hand was given back, and the other one was never touched.
    assert (first.status, first.lease_epoch, first.takeovers) == (S.QUEUED, 1, 0)
    assert (second.status, second.lease_epoch) == (S.QUEUED, 0)


def test_a_stop_that_arrives_between_two_steps_releases_before_the_next_step(stack: Stack) -> None:
    """The stop is already set when the worker gets the job. It starts nothing."""
    script = Cancellable()
    acts = Acts(idempotent=True)

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, Job, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            claim = await claim_job(conn, worker_id="w1")
        assert claim is not None
        stop = asyncio.Event()
        stop.set()
        status = await run_job(engine, script, acts.registry(), claim, stack.settings(), stop=stop)
        return status, *await read(engine, claim)

    status, job, steps = stack.run(body)
    assert status is S.QUEUED
    assert (job.status, job.takeovers) == (S.QUEUED, 0)
    assert steps == []
    assert script.requests == []
    assert acts.runs == []
    # A job that is not final keeps its workspace.
    assert (stack.workspace_root / str(job.id) / "seed.txt").is_file()


def test_a_stop_during_the_check_of_an_interrupted_call_does_not_start_the_rerun(
    stack: Stack,
) -> None:
    """The check says the call did not happen. The stop arrived while it was asked.

    The rerun has no intent yet, so nothing commits the worker to it. The
    job is released with the interrupted call still open.
    """
    script = Cancellable()
    stop = asyncio.Event()
    acts = Acts(hang_at=1)

    def check_and_stop(call: ToolCall) -> CheckAnswer:
        stop.set()
        return CheckOutcome.NOT_DONE

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        first = asyncio.create_task(
            run_job(engine, script, acts.registry(), first_claim, stack.settings())
        )
        await asyncio.to_thread(acts.hanging.wait, 30)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        acts.hang_at = None
        acts.release.set()
        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        schema = {"type": "object"}
        tools = ToolRegistry([Tool("act", "Do it.", schema, acts.run, check=check_and_stop)])
        status = await run_job(engine, script, tools, second_claim, stack.settings(), stop=stop)
        return status, (await read(engine, first_claim))[1]

    status, steps = stack.run(body)
    assert status is S.QUEUED
    assert [step.kind for step in steps] == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.TOOL_INTENT]
    # One run: the abandoned one. The rerun did not start.
    assert len(acts.runs) == 1


def test_a_stop_set_before_a_model_call_means_the_call_is_not_sent(stack: Stack) -> None:
    """The stop arrives while the request step is being written."""
    script = Cancellable()
    acts = Acts(idempotent=True)
    stop = asyncio.Event()

    async def body(
        engine: AsyncEngine, _: AnthropicProvider
    ) -> tuple[JobStatus | None, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            claim = await claim_job(conn, worker_id="w1")
        assert claim is not None

        def on_statement(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
            if "INSERT INTO steps" in statement:
                stop.set()

        event.listen(engine.sync_engine, "before_cursor_execute", on_statement)
        try:
            status = await run_job(
                engine, script, acts.registry(), claim, stack.settings(), stop=stop
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", on_statement)
        return status, (await read(engine, claim))[1]

    status, steps = stack.run(body)
    assert status is S.QUEUED
    assert [step.kind for step in steps] == [K.MODEL_REQUEST]
    # The provider's own record: it was never called.
    assert script.requests == []
    assert script.cancelled == 0
