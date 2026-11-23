"""A provider error that may pass makes the job wait, up to a cap (ADR 0030).

Against real Postgres and the stack's fake provider, whose counted faults
answer the next requests for one model. The fake's request log is the record
of what the worker really sent and how often it was refused.
"""

import asyncio
import dataclasses
import random
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ProviderError
from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.engine import create_engine
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import StatusCause, Step, StepKind, read_steps
from runtime.worker.agent import _CUT, ERROR_TEXT_LENGTH, AgentSettings, run_job
from runtime.worker.backoff import MAX_ERRORS_IN_A_ROW, MAX_ERRORS_IN_JOB, floor

from .conftest import FIXED, HELLO, Stack, expire_lease

MODEL = "hello"
# A scenario of thirty rounds, for a job with many responses.
LONG_MODEL = "loop"
SERVER_ERROR = {"kind": "status", "status": 500}
W, F, S = JobStatus.WAITING, JobStatus.FAILED, JobStatus.SUCCEEDED
# How long the provider client waits in the test of a held request, and how
# long the fake holds it. The hold is the longer, so the client gives up first.
CLIENT_TIMEOUT = 1.0
HOLD_SECONDS = 5.0

# Each fault, and the words of the error the client must have seen for it.
MAY_PASS: dict[str, tuple[dict[str, Any], str]] = {
    "status-408": ({"kind": "status", "status": 408}, "provider answered 408"),
    "status-409": ({"kind": "status", "status": 409}, "provider answered 409"),
    "status-500": ({"kind": "status", "status": 500}, "provider answered 500"),
    "status-502": ({"kind": "status", "status": 502}, "provider answered 502"),
    "status-503": ({"kind": "status", "status": 503}, "provider answered 503"),
    "status-504": ({"kind": "status", "status": 504}, "provider answered 504"),
    "stream-api-error": ({"kind": "stream_error", "error": "api_error"}, "api_error"),
    "stream-timeout-error": ({"kind": "stream_error", "error": "timeout_error"}, "timeout_error"),
    "stream-overloaded": ({"kind": "stream_error", "error": "overloaded_error"}, "overloaded"),
    "stream-rate-limit": ({"kind": "stream_error", "error": "rate_limit_error"}, "rate_limit"),
    "broken-stream": ({"kind": "broken_stream"}, "no complete response"),
    "no-stop-reason": ({"kind": "no_stop_reason"}, "ended before the message was complete"),
    "not-a-stream": ({"kind": "not_a_stream"}, "not a message stream"),
    "client-timeout": ({"kind": "hold", "seconds": HOLD_SECONDS}, "no complete response"),
}
WILL_NOT_PASS: dict[str, dict[str, Any]] = {
    "status-400": {"kind": "status", "status": 400},
    "spend-cap": {"kind": "spend_cap"},
    "stream-spend-cap": {"kind": "stream_spend_cap"},
}


@pytest.fixture
def fault(fake_provider_url: str) -> Iterator[Callable[..., None]]:
    """Fault the next requests for a model. Whatever is left is cleared after the test."""

    def set_fault(count: int, model: str = MODEL, **answer: Any) -> None:
        response = httpx2.put(
            f"{fake_provider_url}/_fake/fault",
            json={"model": model, "count": count, **answer},
            timeout=10,
        )
        assert response.status_code == 204, response.text

    yield set_fault
    httpx2.delete(f"{fake_provider_url}/_fake/fault", timeout=10)


@pytest.fixture
def busy(fake_provider_url: str) -> Iterator[Callable[[bool], None]]:
    """Open or end the fake's busy window for MODEL. It is ended after the test."""

    def set_busy(on: bool) -> None:
        url = f"{fake_provider_url}/_fake/busy"
        if on:
            response = httpx2.put(url, json={"model": MODEL, "seconds": 300}, timeout=10)
        else:
            response = httpx2.delete(url, timeout=10)
        assert response.status_code == 204, response.text

    yield set_busy
    set_busy(False)


def seeded(stack: Stack, model: str = MODEL) -> AgentSettings:
    return dataclasses.replace(stack.settings(model), rng=random.Random(8))  # noqa: S311 - jitter, not a secret


async def run_once(
    engine: AsyncEngine, provider: AnthropicProvider, stack: Stack, model: str = MODEL
) -> JobStatus:
    """Claim the job and run it until this worker leaves it."""
    async with engine.begin() as conn:
        job = await claim_job(conn, worker_id="test-worker")
    assert job is not None
    status = await run_job(engine, provider, FIXED, job, seeded(stack, model))
    assert status is not None
    return status


async def end_the_wait(engine: AsyncEngine, job_id: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE jobs SET not_before = clock_timestamp() WHERE id = :id"), {"id": job_id}
        )


async def run_until_it_ends(
    engine: AsyncEngine,
    provider: AnthropicProvider,
    stack: Stack,
    job: Job,
    most: int,
    model: str = MODEL,
) -> list[JobStatus]:
    """Run the job, ending each wait, for at most `most` claims."""
    statuses: list[JobStatus] = []
    for _ in range(most):
        statuses.append(await run_once(engine, provider, stack, model))
        if statuses[-1] is not W:
            break
        await end_the_wait(engine, job.id)
    return statuses


async def stored(engine: AsyncEngine, job_id: Any) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        job = await get_job(conn, job_id)
        assert job is not None
        return job, await read_steps(conn, job_id)


def waits(steps: list[Step]) -> list[dict[str, Any]]:
    return [
        step.payload
        for step in steps
        if step.kind is StepKind.STATUS_CHANGED and step.payload["to"] == "waiting"
    ]


def faulted(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The requests the fake answered with a fault, by its own log."""
    return [entry for entry in requests if entry.get("fault") is not None]


def with_client_timeout[T](
    stack: Stack,
    timeout: float,
    body: Callable[[AsyncEngine, AnthropicProvider], Awaitable[T]],
) -> T:
    """`stack.run`, with a provider client that gives up after `timeout` seconds."""

    async def go() -> T:
        engine = create_engine(stack.database_url)
        http = httpx2.AsyncClient(timeout=timeout)
        provider = AnthropicProvider(
            api_key="test", base_url=stack.fake_provider_url, http_client=http
        )
        try:
            return await body(engine, provider)
        finally:
            await provider.aclose()
            await http.aclose()
            await engine.dispose()

    return asyncio.run(go())


@pytest.mark.parametrize(("answer", "words"), MAY_PASS.values(), ids=MAY_PASS.keys())
def test_an_error_that_may_pass_makes_the_job_wait_and_then_finish(
    stack: Stack, fault: Any, answer: dict[str, Any], words: str
) -> None:
    fault(1, **answer)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        first = await run_once(engine, provider, stack)
        # Here, before the second claim: a job that failed has nothing to claim.
        assert first is JobStatus.WAITING
        waited = await stored(engine, job.id)
        await end_the_wait(engine, job.id)
        second = await run_once(engine, provider, stack)
        return job, waited, second, await stored(engine, job.id)

    timeout = CLIENT_TIMEOUT if answer["kind"] == "hold" else 30.0
    job, (waiting, waited_steps), second, (final, _) = with_client_timeout(stack, timeout, body)

    assert waiting.status is JobStatus.WAITING
    assert waiting.lease_owner is None
    [wait] = waits(waited_steps)
    assert wait["cause"] == "provider_error"
    assert wait["attempt"] == 1
    assert words in wait["error"]
    assert second is JobStatus.SUCCEEDED
    assert final.status is JobStatus.SUCCEEDED

    # By the fake's log: one fault, then the same request again, to the byte.
    requests = stack.requests_for(job)
    assert [entry.get("fault") for entry in requests] == [answer["kind"], None, None]
    assert requests[1]["body_sha256"] == requests[0]["body_sha256"]


def test_a_retry_after_on_an_error_sets_the_floor_of_the_wait(stack: Stack, fault: Any) -> None:
    fault(1, kind="status", status=503, retry_after=7)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> list[Step]:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        assert await run_once(engine, provider, stack) is W
        return (await stored(engine, job.id))[1]

    [wait] = waits(stack.run(body))
    assert wait["retry_after"] == 7
    assert wait["delay_seconds"] >= floor(1, 7)


def test_ten_errors_in_a_row_are_waited_out_and_the_job_succeeds(stack: Stack, fault: Any) -> None:
    fault(MAX_ERRORS_IN_A_ROW, **SERVER_ERROR)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        statuses = await run_until_it_ends(engine, provider, stack, job, MAX_ERRORS_IN_A_ROW + 2)
        return job, statuses, await stored(engine, job.id)

    job, statuses, (final, steps) = stack.run(body)
    assert statuses == [W] * MAX_ERRORS_IN_A_ROW + [S]
    assert final.status is S
    assert [wait["attempt"] for wait in waits(steps)] == list(range(1, MAX_ERRORS_IN_A_ROW + 1))

    requests = stack.requests_for(job)
    assert len(faulted(requests)) == MAX_ERRORS_IN_A_ROW
    # The ten faults and their retry are one request, then the job's second turn.
    assert len(requests) == MAX_ERRORS_IN_A_ROW + 2
    assert len({entry["body_sha256"] for entry in requests[:-1]}) == 1


def test_the_eleventh_error_in_a_row_fails_the_job_and_names_the_cap(
    stack: Stack, fault: Any
) -> None:
    fault(MAX_ERRORS_IN_A_ROW + 5, **SERVER_ERROR)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        statuses = await run_until_it_ends(engine, provider, stack, job, MAX_ERRORS_IN_A_ROW + 3)
        return job, statuses, await stored(engine, job.id)

    job, statuses, (final, steps) = stack.run(body)
    assert statuses == [JobStatus.WAITING] * 10 + [JobStatus.FAILED]
    assert final.status is F
    assert final.error is not None
    assert f"{MAX_ERRORS_IN_A_ROW} provider errors in a row" in final.error
    assert "provider answered 500" in final.error
    assert len(waits(steps)) == MAX_ERRORS_IN_A_ROW

    requests = stack.requests_for(job)
    assert len(requests) == MAX_ERRORS_IN_A_ROW + 1
    assert len(faulted(requests)) == MAX_ERRORS_IN_A_ROW + 1


@pytest.mark.parametrize("kind", ["overloaded_error", "rate_limit_error"])
def test_a_busy_error_inside_a_stream_counts_toward_the_cap(
    stack: Stack, fault: Any, kind: str
) -> None:
    """Output has begun, so the call can be billed: it does not wait without a cap."""
    fault(MAX_ERRORS_IN_A_ROW + 5, kind="stream_error", error=kind)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        statuses = await run_until_it_ends(engine, provider, stack, job, MAX_ERRORS_IN_A_ROW + 3)
        return job, statuses, await stored(engine, job.id)

    job, statuses, (final, steps) = stack.run(body)
    assert statuses == [JobStatus.WAITING] * 10 + [JobStatus.FAILED]
    assert {wait["cause"] for wait in waits(steps)} == {"provider_error"}
    assert final.error is not None
    assert f"{MAX_ERRORS_IN_A_ROW} provider errors in a row" in final.error
    assert len(faulted(stack.requests_for(job))) == MAX_ERRORS_IN_A_ROW + 1


@pytest.mark.parametrize(
    ("retryable", "claims"), [(False, 1), (True, MAX_ERRORS_IN_A_ROW + 1)], ids=["fatal", "at-cap"]
)
def test_the_text_that_fails_a_job_is_cut(stack: Stack, retryable: bool, claims: int) -> None:
    """A proxy's error page does not go whole into the append-only log."""
    page = "provider answered 502: " + "x" * 200_000

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)

        async def complete(request: Any) -> Any:
            raise ProviderError(page, status=502, retryable=retryable)

        provider.complete = complete  # type: ignore[method-assign]
        statuses = await run_until_it_ends(engine, provider, stack, job, claims)
        return statuses, await stored(engine, job.id)

    statuses, (final, steps) = stack.run(body)
    assert statuses[-1] is JobStatus.FAILED
    assert final.error is not None
    assert len(final.error) == ERROR_TEXT_LENGTH
    assert final.error.endswith(_CUT)
    if retryable:
        assert f"{MAX_ERRORS_IN_A_ROW} provider errors in a row" in final.error
    [failed] = [step for step in steps if step.kind is StepKind.ERROR]
    assert failed.payload["message"] == final.error


def test_the_job_cap_counts_errors_across_responses(stack: Stack, fault: Any) -> None:
    """Ten errors before each response: the 31st error of the job fails it."""
    armed: set[int] = set()

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, {**HELLO, "model": LONG_MODEL})
        original = provider.complete

        async def complete(request: Any) -> Any:
            # The first time a conversation of this length is sent, the next
            # ten requests are faulted: each response costs ten errors.
            if len(request.messages) not in armed:
                armed.add(len(request.messages))
                fault(MAX_ERRORS_IN_A_ROW, model=LONG_MODEL, **SERVER_ERROR)
            return await original(request)

        provider.complete = complete  # type: ignore[method-assign]
        statuses = await run_until_it_ends(
            engine, provider, stack, job, MAX_ERRORS_IN_JOB + 5, LONG_MODEL
        )
        return job, statuses, await stored(engine, job.id)

    job, statuses, (final, _) = stack.run(body)
    assert statuses == [JobStatus.WAITING] * 30 + [JobStatus.FAILED]
    assert final.error is not None
    assert f"{MAX_ERRORS_IN_JOB} provider errors in the job" in final.error

    requests = stack.requests_for(job)
    assert len(faulted(requests)) == MAX_ERRORS_IN_JOB + 1
    # Three answers came through, one after each ten errors.
    assert sum(1 for entry in requests if entry.get("fault") is None) == 3


def test_a_busy_answer_between_errors_does_not_reset_the_count(
    stack: Stack, fault: Any, busy: Any
) -> None:
    """Otherwise a provider that alternates 429 and 500 never reaches the cap."""

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        statuses: list[JobStatus] = []
        for answer in ["error"] * 5 + ["busy"] + ["error"] * 6:
            if answer == "busy":
                busy(True)
            else:
                fault(1, **SERVER_ERROR)
            statuses.append(await run_once(engine, provider, stack))
            busy(False)
            if statuses[-1] is W:
                await end_the_wait(engine, job.id)
        return job, statuses, await stored(engine, job.id)

    job, statuses, (final, steps) = stack.run(body)
    assert statuses == [JobStatus.WAITING] * 11 + [JobStatus.FAILED]
    assert final.error is not None
    assert f"{MAX_ERRORS_IN_A_ROW} provider errors in a row" in final.error
    # A wait of either cause makes the next one longer.
    assert [wait["attempt"] for wait in waits(steps)] == list(range(1, 12))
    assert [entry["status"] for entry in stack.requests_for(job)] == ([500] * 5 + [429] + [500] * 6)


def test_the_counts_survive_a_takeover_by_another_worker(stack: Stack, fault: Any) -> None:
    """A worker that stops after nine waits leaves the count in the log."""
    fault(MAX_ERRORS_IN_A_ROW + 5, **SERVER_ERROR)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        before = await run_until_it_ends(engine, provider, stack, job, MAX_ERRORS_IN_A_ROW - 1)
        # A worker claims the job and stops without a word: its lease runs out.
        async with engine.begin() as conn:
            assert await claim_job(conn, worker_id="stopped-worker") is not None
            await expire_lease(conn, job.id)
        after = await run_until_it_ends(engine, provider, stack, job, 3)
        return job, before, after, await stored(engine, job.id)

    job, before, after, (final, _) = stack.run(body)
    assert before == [W] * (MAX_ERRORS_IN_A_ROW - 1)
    assert final.takeovers == 1
    assert after == [JobStatus.WAITING, JobStatus.FAILED]
    assert final.error is not None
    assert f"{MAX_ERRORS_IN_A_ROW} provider errors in a row" in final.error
    assert len(faulted(stack.requests_for(job))) == MAX_ERRORS_IN_A_ROW + 1


@pytest.mark.parametrize("answer", WILL_NOT_PASS.values(), ids=WILL_NOT_PASS.keys())
def test_an_error_that_will_not_pass_fails_the_job_at_once(
    stack: Stack, fault: Any, answer: dict[str, Any]
) -> None:
    fault(5, **answer)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
        status = await run_once(engine, provider, stack)
        return job, status, await stored(engine, job.id)

    job, status, (final, steps) = stack.run(body)
    assert status is JobStatus.FAILED
    assert final.error is not None
    assert final.error.startswith("model call failed: ")
    assert waits(steps) == []
    assert len(stack.requests_for(job)) == 1


def test_a_job_whose_cancel_is_requested_does_not_wait_on_an_error(
    stack: Stack, fault: Any
) -> None:
    fault(1, **SERVER_ERROR)

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Any:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
            claimed = await claim_job(conn, worker_id="w")
        assert claimed is not None
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET cancel_requested = true WHERE id = :id"), {"id": job.id}
            )
        status = await run_job(engine, provider, FIXED, claimed, seeded(stack))
        return status, await stored(engine, job.id)

    status, (final, steps) = stack.run(body)
    assert status is JobStatus.CANCELLED
    assert (final.status, final.not_before) == (JobStatus.CANCELLED, None)
    assert waits(steps) == []


def test_a_wait_takes_a_provider_cause_only(stack: Stack) -> None:
    wrong = held.Wait(
        seconds=1, attempt=1, retry_after=None, error="x", cause=StatusCause.CANCEL_REQUESTED
    )

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> None:
        async with engine.begin() as conn:
            job = await stack.create(conn, HELLO)
            claimed = await claim_job(conn, worker_id="w")
        assert claimed is not None
        with pytest.raises(ValueError, match="not a wait"):
            await held.leave_running(
                engine,
                job_id=job.id,
                lease_epoch=claimed.lease_epoch,
                new_status=JobStatus.WAITING,
                wait=wrong,
            )

    stack.run(body)
