"""G7, cancel and deadline: a job stops within 5 s, also in the middle of a model call.

The provider never answers, so the job is in the middle of the stream when
the cancel or the deadline comes. The time is measured from the request, or
the deadline, to the status change in the database.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import DEADLINE_ERROR, Job, cancel_job, claim_job, create_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind, read_steps
from runtime.tools import default_tools
from runtime.worker.agent import run_job

from .conftest import Stack, database_now

# The pass line of G7.
WITHIN = 5.0


class Silent:
    """A provider that takes the request and never answers."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self.abandoned = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.asked.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.abandoned.set()
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        return None


async def start(engine: AsyncEngine, stack: Stack, deadline_in: timedelta) -> Job:
    async with engine.begin() as conn:
        await create_job(
            conn,
            spec={"prompt": "go"},
            token_budget=100_000,
            deadline=datetime.now(UTC) + deadline_in,
        )
        claimed = await claim_job(conn, worker_id="w1")
    assert claimed is not None
    return claimed


def test_a_cancel_stops_a_job_in_the_middle_of_a_model_call(stack: Stack) -> None:
    provider = Silent()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> tuple[float, Job, bool]:
        job = await start(engine, stack, timedelta(hours=1))
        run = asyncio.create_task(run_job(engine, provider, default_tools(), job, stack.settings()))
        await asyncio.wait_for(provider.asked.wait(), 10)
        asked = time.monotonic()
        async with engine.begin() as conn:
            requested, done = await cancel_job(conn, job.id)
        assert (done, requested.status) == (False, JobStatus.RUNNING)
        status = await asyncio.wait_for(run, 30)
        took = time.monotonic() - asked
        assert status is JobStatus.CANCELLED
        async with engine.connect() as conn:
            final = await get_job(conn, job.id)
        assert final is not None
        return took, final, provider.abandoned.is_set()

    took, job, abandoned = stack.run(body)

    assert job.status is JobStatus.CANCELLED
    assert abandoned
    assert took < WITHIN


def test_the_deadline_stops_a_job_in_the_middle_of_a_model_call(stack: Stack) -> None:
    provider = Silent()

    async def body(engine: AsyncEngine, _: AnthropicProvider) -> tuple[float, Job, list[str]]:
        job = await start(engine, stack, timedelta(seconds=2))
        # Measured as the worker keeps the deadline: the time left read once
        # from the database clock, then counted on the monotonic clock. The
        # wall clock can be stepped during the wait, by an NTP or VM time sync.
        async with engine.connect() as conn:
            left = (job.deadline - await database_now(conn)).total_seconds()
        started = time.monotonic()
        status = await asyncio.wait_for(
            run_job(engine, provider, default_tools(), job, stack.settings()), 30
        )
        late = time.monotonic() - started - left
        assert status is JobStatus.FAILED
        async with engine.connect() as conn:
            final = await get_job(conn, job.id)
            steps = await read_steps(conn, job.id)
        assert final is not None
        return late, final, [s.kind.value for s in steps]

    late, job, kinds = stack.run(body)

    assert (job.status, job.error) == (JobStatus.FAILED, DEADLINE_ERROR)
    # The request was sent and given up; the failure has its step.
    assert kinds == [StepKind.MODEL_REQUEST.value, StepKind.ERROR.value]
    assert provider.abandoned.is_set()
    assert 0 <= late < WITHIN
