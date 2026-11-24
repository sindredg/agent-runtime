"""A job that repeats one call with the same result five times in a row fails (ADR 0031).

Against real Postgres and the stack's fake provider. The `stuck` scenario
reads notes.txt every turn, eight times. The fake's request log is the record
of how many model calls the worker really sent.
"""

import asyncio
import threading
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.tools.base import ToolCall, ToolResult
from runtime.tools.read_file import READ_FILE
from runtime.worker.agent import run_job
from runtime.worker.replay import MAX_REPEATS

from .conftest import Stack, expire_lease, stub_tool

SPEC: dict[str, Any] = {
    "prompt": "Read notes.txt until you are done.",
    "model": "stuck",
    "files": {"notes.txt": "the same notes\n"},
}
# The scenario's reads, and its model calls: one per read and the final answer.
READS = 8
ERROR = (
    f"the job repeated the same call to read_file with the same result {MAX_REPEATS} times in a row"
)


def answered(stack: Stack, job: Job) -> list[dict[str, Any]]:
    return [entry for entry in stack.requests_for(job) if entry.get("status") == 200]


def results(steps: list[Step]) -> list[dict[str, Any]]:
    return [step.payload for step in steps if step.kind is StepKind.TOOL_RESULT]


def test_a_job_stuck_on_one_call_fails_after_five_identical_results(stack: Stack) -> None:
    ran = stack.run_one(SPEC, default_tools())
    calls = answered(stack, ran.job)

    # By the fake provider's own count: the call after the fifth result was never sent.
    assert len(calls) == MAX_REPEATS
    assert ran.job.status == "failed"
    assert ran.job.error == ERROR
    assert len(results(ran.steps)) == MAX_REPEATS
    assert {result["content"] for result in results(ran.steps)} == {"the same notes\n"}
    # The job fails before the request step of the next call.
    assert ran.kinds[-2:] == [StepKind.TOOL_RESULT, StepKind.ERROR]
    assert ran.steps[-1].payload["message"] == ERROR
    assert ran.kinds.count(StepKind.MODEL_REQUEST) == MAX_REPEATS


def test_a_worker_that_takes_over_after_four_results_stops_the_job_after_the_fifth(
    stack: Stack,
) -> None:
    """The count comes from the log, so the next worker continues it."""

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Step]]:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        reads = 0

        def read(call: ToolCall) -> ToolResult:
            nonlocal reads
            reads += 1
            if reads == MAX_REPEATS - 1:
                # The first worker is told to stop during its fourth read. It
                # stores the result and gives the job back.
                loop.call_soon_threadsafe(stop.set)
            return ToolResult("the same notes\n")

        tools = stub_tool(read)
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first = await claim_job(conn, worker_id="first-worker")
        assert first is not None
        status = await run_job(engine, provider, tools, first, stack.settings(), stop=stop)
        assert status is JobStatus.QUEUED
        # A second worker claims the job and stops without a word: its lease runs out.
        async with engine.begin() as conn:
            assert await claim_job(conn, worker_id="stopped-worker") is not None
            await expire_lease(conn, first.id)
            third = await claim_job(conn, worker_id="third-worker")
        assert third is not None
        assert third.takeovers == 1
        await run_job(engine, provider, tools, third, stack.settings())
        async with engine.begin() as conn:
            job = await get_job(conn, first.id)
            assert job is not None
            return job, await read_steps(conn, job.id)

    job, steps = stack.run(body)
    calls = answered(stack, job)

    assert len(calls) == MAX_REPEATS
    assert job.status == "failed"
    assert job.error == ERROR
    assert job.takeovers == 1
    assert len(results(steps)) == MAX_REPEATS


def test_four_identical_results_then_a_different_one_and_the_job_goes_on(stack: Stack) -> None:
    """The fifth read gets a new answer, and the count starts again from it."""
    lock = threading.Lock()
    reads = 0

    def read(call: ToolCall) -> ToolResult:
        nonlocal reads
        with lock:
            reads += 1
            return ToolResult("old notes\n" if reads < MAX_REPEATS else "new notes\n")

    ran = stack.run_one(SPEC, stub_tool(read))

    assert ran.job.status == "succeeded", ran.job.error
    contents = [result["content"] for result in results(ran.steps)]
    assert contents == ["old notes\n"] * 4 + ["new notes\n"] * 4
    assert len(answered(stack, ran.job)) == READS + 1


def test_the_same_call_on_a_file_that_changes_between_reads_is_not_stopped(stack: Stack) -> None:
    lock = threading.Lock()

    def read_a_growing_file(call: ToolCall) -> ToolResult:
        with lock:
            with (call.workspace / "notes.txt").open("a") as notes:
                notes.write("one more line\n")
            return READ_FILE.run(call)

    ran = stack.run_one(SPEC, stub_tool(read_a_growing_file))

    assert ran.job.status == "succeeded", ran.job.error
    contents = [result["content"] for result in results(ran.steps)]
    assert len(contents) == READS
    assert len(set(contents)) == READS
    assert len(answered(stack, ran.job)) == READS + 1
