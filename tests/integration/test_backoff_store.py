"""A job that waits for a busy provider, in the database (ADR 0018).

The holder moves it to `waiting` with `not_before` from the database clock
and a `status_changed` step, in one statement. The claim leaves it alone
until `not_before` has passed. A cancel and the deadline still end it.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runtime.store import held
from runtime.store.chain import verify_job
from runtime.store.jobs import DEADLINE_ERROR, Job, cancel_job, claim_job, fail_overdue, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps

from .conftest import database_now, new_job, with_engine

S = JobStatus
WAIT = held.Wait(seconds=30, attempt=2, retry_after=5, error="provider answered 429: busy")


async def held_job(engine: AsyncEngine) -> Job:
    async with engine.begin() as conn:
        await new_job(conn)
        job = await claim_job(conn, worker_id="w1", lease=timedelta(minutes=5))
    assert job is not None
    return job


async def wait(engine: AsyncEngine, job: Job, how: held.Wait = WAIT) -> Job:
    return await held.leave_running(
        engine, job_id=job.id, lease_epoch=job.lease_epoch, new_status=S.WAITING, wait=how
    )


async def end_the_wait(conn: AsyncConnection, job: Job) -> None:
    """Move `not_before` into the past, as if the wait had run out."""
    await conn.execute(
        text("UPDATE jobs SET not_before = clock_timestamp() - interval '1 second' WHERE id = :id"),
        {"id": job.id},
    )


async def stored(engine: AsyncEngine, job: Job) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        found = await get_job(conn, job.id)
        assert found is not None
        assert (await verify_job(conn, job.id)).mismatches == []
        return found, await read_steps(conn, job.id)


def test_a_wait_sets_not_before_by_the_database_clock_and_writes_its_step(
    database_url: str,
) -> None:
    async def body(engine: AsyncEngine) -> tuple[datetime, Job, Job, list[Step], datetime]:
        job = await held_job(engine)
        async with engine.connect() as conn:
            before = await database_now(conn)
        left = await wait(engine, job)
        async with engine.connect() as conn:
            after = await database_now(conn)
        return before, left, *await stored(engine, job), after

    before, left, job, steps, after = with_engine(database_url, body)
    assert left == job
    assert job.status is S.WAITING
    assert (job.lease_owner, job.lease_expires_at, job.error) == (None, None, None)
    assert job.not_before is not None
    assert before + timedelta(seconds=30) <= job.not_before <= after + timedelta(seconds=30)

    [step] = steps
    assert step.kind is StepKind.STATUS_CHANGED
    assert step.lease_epoch == 1
    assert step.payload == {
        "from": "running",
        "to": "waiting",
        "cause": "provider_busy",
        "attempt": 2,
        "delay_seconds": 30,
        "retry_after": 5,
        "error": "provider answered 429: busy",
        "not_before": step.payload["not_before"],
    }
    assert datetime.fromisoformat(step.payload["not_before"]) == job.not_before


def test_a_wait_while_a_cancel_is_requested_is_the_cancel(database_url: str) -> None:
    """Nobody would stop a waiting job before its `not_before` otherwise (G7)."""

    async def body(engine: AsyncEngine) -> tuple[Job, list[Step]]:
        job = await held_job(engine)
        async with engine.begin() as conn:
            await cancel_job(conn, job.id)
        await wait(engine, job)
        return await stored(engine, job)

    job, steps = with_engine(database_url, body)
    assert (job.status, job.not_before) == (S.CANCELLED, None)
    assert [step.payload for step in steps] == [
        {"from": "running", "to": "cancelled", "cause": "cancel_requested"}
    ]


def test_the_claim_leaves_a_waiting_job_until_its_not_before(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job | None, Job | None]:
        job = await held_job(engine)
        await wait(engine, job)
        async with engine.begin() as conn:
            early = await claim_job(conn, worker_id="w2")
        async with engine.begin() as conn:
            await end_the_wait(conn, job)
            late = await claim_job(conn, worker_id="w2")
        return early, late

    early, late = with_engine(database_url, body)
    assert early is None
    assert late is not None
    assert (late.status, late.lease_owner, late.not_before) == (S.RUNNING, "w2", None)
    # A claim after a wait is not a takeover; the epoch still fences the first holder.
    assert (late.lease_epoch, late.takeovers) == (2, 0)


def test_a_waiting_job_without_not_before_is_not_claimed(database_url: str) -> None:
    """Only a hand-made row has no `not_before`. Its deadline ends it."""

    async def body(engine: AsyncEngine) -> Job | None:
        job = await held_job(engine)
        await wait(engine, job)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET not_before = NULL WHERE id = :id"), {"id": job.id}
            )
            return await claim_job(conn, worker_id="w2")

    assert with_engine(database_url, body) is None


def test_a_waiting_job_is_cancelled_at_once(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[bool, Job, list[Step]]:
        job = await held_job(engine)
        await wait(engine, job)
        async with engine.begin() as conn:
            _, done = await cancel_job(conn, job.id)
        return done, *await stored(engine, job)

    done, job, steps = with_engine(database_url, body)
    assert done is True
    assert job.status is S.CANCELLED
    assert steps[-1].payload == {"from": "waiting", "to": "cancelled", "cause": "cancel_requested"}


def test_a_waiting_job_past_its_deadline_is_failed_before_its_wait_ends(
    database_url: str,
) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[Job], Job, list[Step]]:
        job = await held_job(engine)
        await wait(engine, job, held.Wait(seconds=3600, attempt=1, retry_after=None, error="x"))
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET deadline = clock_timestamp() WHERE id = :id"),
                {"id": job.id},
            )
            ended = await fail_overdue(conn)
        return ended, *await stored(engine, job)

    ended, job, steps = with_engine(database_url, body)
    assert [found.id for found in ended] == [job.id]
    assert (job.status, job.error) == (S.FAILED, DEADLINE_ERROR)
    assert steps[-1].kind is StepKind.ERROR


@pytest.mark.parametrize(
    ("status", "how"),
    [(S.WAITING, None), (S.QUEUED, WAIT), (S.NEEDS_APPROVAL, WAIT)],
    ids=["waiting-without-wait", "queued-with-wait", "park-with-wait"],
)
def test_a_wait_goes_with_the_move_to_waiting_only(
    database_url: str, status: JobStatus, how: held.Wait | None
) -> None:
    async def body(engine: AsyncEngine) -> None:
        job = await held_job(engine)
        await held.leave_running(engine, job_id=job.id, lease_epoch=1, new_status=status, wait=how)

    with pytest.raises(ValueError, match="takes a wait"):
        with_engine(database_url, body)


@pytest.mark.parametrize(
    "how",
    [
        held.Wait(seconds=-1, attempt=1, retry_after=None, error="x"),
        held.Wait(seconds=float("inf"), attempt=1, retry_after=None, error="x"),
        held.Wait(seconds=1, attempt=0, retry_after=None, error="x"),
    ],
    ids=["negative", "infinite", "attempt-0"],
)
def test_a_wait_that_cannot_be_is_refused_before_it_is_sent(
    database_url: str, how: held.Wait
) -> None:
    async def body(engine: AsyncEngine) -> Job:
        job = await held_job(engine)
        with pytest.raises(ValueError, match="not a wait"):
            await wait(engine, job, how)
        return (await stored(engine, job))[0]

    job = with_engine(database_url, body)
    assert job.status is S.RUNNING


def test_not_before_is_never_the_workers_clock(database_url: str) -> None:
    """The statement carries the delay, not a time: a worker clock that is off changes nothing."""

    async def body(engine: AsyncEngine) -> tuple[Job, datetime]:
        job = await held_job(engine)
        left = await wait(engine, job, held.Wait(seconds=0, attempt=1, retry_after=None, error="x"))
        async with engine.connect() as conn:
            return left, await database_now(conn)

    left, now = with_engine(database_url, body)
    assert left.not_before is not None
    assert left.not_before <= now
    assert abs(left.not_before - datetime.now(UTC)) < timedelta(minutes=5)
