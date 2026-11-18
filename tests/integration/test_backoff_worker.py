"""The worker waits out a busy provider instead of failing the job (ADR 0018).

Against real Postgres and the stack's fake provider, whose busy window
answers 429 or 529 for one model. The fake's request log is the record of
what the worker really sent.
"""

import dataclasses
import random
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.worker.agent import AgentSettings, run_job
from runtime.worker.backoff import ceiling, floor

from .conftest import FIXED, HELLO, Stack

K = StepKind
# Its own scenario name, so that the busy window refuses no other test's job.
MODEL = "hello"


@pytest.fixture
def busy(fake_provider_url: str) -> Iterator[Any]:
    """Set the fake's busy window for MODEL; it is ended after the test."""

    def set_busy(**window: Any) -> None:
        response = httpx2.put(
            f"{fake_provider_url}/_fake/busy", json={"model": MODEL, **window}, timeout=10
        )
        assert response.status_code == 204, response.text

    yield set_busy
    httpx2.delete(f"{fake_provider_url}/_fake/busy", timeout=10)


def seeded(stack: Stack, model: str = MODEL) -> AgentSettings:
    return dataclasses.replace(stack.settings(model), rng=random.Random(8))  # noqa: S311 - jitter, not a secret


async def run_once(engine: AsyncEngine, provider: AnthropicProvider, stack: Stack) -> JobStatus:
    async with engine.begin() as conn:
        job = await claim_job(conn, worker_id="test-worker")
    assert job is not None
    status = await run_job(engine, provider, FIXED, job, seeded(stack))
    assert status is not None
    return status


async def end_the_wait(engine: AsyncEngine, job_id: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET not_before = clock_timestamp() WHERE id = :id"), {"id": job_id}
        )


async def stored(engine: AsyncEngine, job_id: Any) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        job = await get_job(conn, job_id)
        assert job is not None
        return job, await read_steps(conn, job_id)


def waits(steps: list[Step]) -> list[dict[str, Any]]:
    return [
        step.payload
        for step in steps
        if step.kind is K.STATUS_CHANGED and step.payload["to"] == "waiting"
    ]


@pytest.mark.parametrize(
    ("status", "cause"), [(429, "rate_limit_error"), (529, "overloaded_error")]
)
def test_a_busy_provider_makes_the_job_wait_and_then_finish(
    stack: Stack, busy: Any, status: int, cause: str
) -> None:
    busy(seconds=300, status=status, retry_after=5)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        first = await run_once(engine, provider, stack)
        waited, waited_steps = await stored(engine, job.id)
        async with engine.begin() as conn:
            early = await claim_job(conn, worker_id="early")
        busy(seconds=300, status=status, retry_after=5)
        await end_the_wait(engine, job.id)
        second = await run_once(engine, provider, stack)
        after_second = await stored(engine, job.id)
        httpx2.delete(f"{stack.fake_provider_url}/_fake/busy", timeout=10)
        await end_the_wait(engine, job.id)
        third = await run_once(engine, provider, stack)
        return job, first, waited, waited_steps, early, second, after_second, third

    job, first, waited, waited_steps, early, second, after_second, third = stack.run(body)

    # The first busy answer: the job waits, the request step has no response.
    assert first is JobStatus.WAITING
    assert waited.status is JobStatus.WAITING
    assert waited.lease_owner is None
    assert [step.kind for step in waited_steps] == [K.MODEL_REQUEST, K.STATUS_CHANGED]
    [wait] = waits(waited_steps)
    assert wait["cause"] == "provider_busy"
    assert wait["attempt"] == 1
    assert wait["retry_after"] == 5
    assert str(status) in wait["error"]
    assert floor(1, 5) <= wait["delay_seconds"] <= floor(1, 5) + ceiling(1) / 2
    # Nobody claims it before its wait is over.
    assert early is None

    # The second, in a row: the attempt counts on, and the wait grows.
    assert second is JobStatus.WAITING
    assert [w["attempt"] for w in waits(after_second[1])] == [1, 2]

    # The provider is back: the job runs to the end from where it stood.
    assert third is JobStatus.SUCCEEDED
    final, steps = stack.run(lambda engine, provider: stored(engine, job.id))
    assert final.status is JobStatus.SUCCEEDED
    assert final.not_before is None
    assert [step.kind for step in steps] == [
        K.MODEL_REQUEST,
        K.STATUS_CHANGED,
        K.MODEL_REQUEST,
        K.STATUS_CHANGED,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
    ]
    # Three claims after the first: none was a takeover.
    assert (final.lease_epoch, final.takeovers) == (3, 0)
    # The waits cost no tokens: only the two answers were counted.
    usage = [s.payload["usage"] for s in steps if s.kind is K.MODEL_RESPONSE]
    assert final.tokens_used == sum(u["input_tokens"] + u["output_tokens"] for u in usage)

    requests = stack.requests_for(job)
    assert [entry["status"] for entry in requests] == [status, status, 200, 200]
    assert {entry["error"] for entry in requests[:2]} == {cause}


def test_the_wait_counts_again_after_a_response(stack: Stack, busy: Any) -> None:
    """Only busy answers in a row make the wait grow."""

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> list[dict[str, Any]]:
        async with engine.begin() as conn:
            job = await stack.create(conn, {**HELLO, "model": "ci-triage"})
        settings = seeded(stack, "ci-triage")
        # The first call is answered; the window then refuses the second.
        async with engine.begin() as conn:
            claimed = await claim_job(conn, worker_id="w")
        assert claimed is not None

        provider_calls = 0
        original = provider.complete

        async def complete(request: Any) -> Any:
            nonlocal provider_calls
            provider_calls += 1
            if provider_calls == 2:
                httpx2.put(
                    f"{stack.fake_provider_url}/_fake/busy",
                    json={"model": "ci-triage", "seconds": 300},
                    timeout=10,
                )
            return await original(request)

        provider.complete = complete  # type: ignore[method-assign]
        status = await run_job(engine, provider, FIXED, claimed, settings)
        assert status is JobStatus.WAITING
        return waits((await stored(engine, job.id))[1])

    try:
        [wait] = stack.run(body)
    finally:
        httpx2.delete(f"{stack.fake_provider_url}/_fake/busy", timeout=10)
    assert wait["attempt"] == 1
    assert wait["retry_after"] is None
    assert 1 <= wait["delay_seconds"] <= 2


def test_a_job_whose_cancel_is_requested_does_not_wait(stack: Stack, busy: Any) -> None:
    """A wait of 60 s would hold a cancelled job past G7's 5 s. It is cancelled instead."""
    busy(seconds=300, retry_after=60)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Any, Job]:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
            claimed = await claim_job(conn, worker_id="w", lease=timedelta(minutes=5))
        assert claimed is not None
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET cancel_requested = true WHERE id = :id"), {"id": job.id}
            )
        status = await run_job(engine, provider, FIXED, claimed, seeded(stack))
        return status, (await stored(engine, job.id))[0]

    status, job = stack.run(body)
    assert status is JobStatus.CANCELLED
    assert (job.status, job.not_before) == (JobStatus.CANCELLED, None)
