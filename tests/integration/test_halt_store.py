"""Cancel and deadline in the store, on real Postgres (ADR 0010).

A job no worker holds is cancelled in one statement; a running job gets the
request. A claim ends a job whose cancel was requested or whose deadline
passed instead of handing it out. Jobs that wait past their deadline are
failed by the workers' timer.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from sqlalchemy import text, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runtime.store import held
from runtime.store.jobs import (
    DEADLINE_ERROR,
    InvalidTransition,
    Job,
    JobNotFound,
    cancel_job,
    cancel_wanted,
    claim_job,
    create_job,
    fail_overdue,
    get_job,
    overdue_statement,
)
from runtime.store.schema import jobs
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind, read_steps
from runtime.worker.loop import keep_failing_overdue

from .conftest import ask, database_now, expire_lease, with_engine

S = JobStatus
LATER = timedelta(hours=1)


async def new(conn: AsyncConnection, *, deadline_in: timedelta = LATER) -> Job:
    return await create_job(
        conn, spec={"prompt": "go"}, token_budget=1000, deadline=datetime.now(UTC) + deadline_in
    )


async def put(conn: AsyncConnection, job: Job, **values: Any) -> None:
    await conn.execute(update(jobs).where(jobs.c.id == job.id).values(**values))


async def past_deadline(conn: AsyncConnection, job: Job) -> None:
    await conn.execute(
        text("UPDATE jobs SET deadline = clock_timestamp() - interval '1 second' WHERE id = :id"),
        {"id": job.id},
    )


@pytest.mark.parametrize("status", [S.QUEUED, S.WAITING, S.NEEDS_APPROVAL])
def test_a_job_nobody_holds_is_cancelled_at_once(database_url: str, status: JobStatus) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, bool, Job | None]:
        async with engine.begin() as conn:
            job = await new(conn)
            await put(conn, job, status=status.value)
        async with engine.begin() as conn:
            cancelled, done = await cancel_job(conn, job.id)
            return cancelled, done, await claim_job(conn, worker_id="w1")

    cancelled, done, claimed = with_engine(database_url, body)

    assert done is True
    assert cancelled.status is S.CANCELLED
    assert claimed is None


def test_a_running_job_gets_the_request_and_keeps_running(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, bool, bool]:
        async with engine.begin() as conn:
            await new(conn)
            held = await claim_job(conn, worker_id="w1")
        assert held is not None
        async with engine.begin() as conn:
            job, done = await cancel_job(conn, held.id)
        async with engine.connect() as conn:
            return job, done, await cancel_wanted(conn, held.id)

    job, done, wanted = with_engine(database_url, body)

    assert done is False
    assert job.status is S.RUNNING
    assert job.cancel_requested is True
    assert wanted is True


def test_a_final_job_and_an_unknown_one_are_refused(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> list[type[Exception]]:
        raised: list[type[Exception]] = []
        async with engine.begin() as conn:
            job = await new(conn)
            await put(conn, job, status=S.SUCCEEDED.value)
        for job_id in (job.id, uuid.UUID(int=1)):
            try:
                async with engine.begin() as conn:
                    await cancel_job(conn, job_id)
            except (InvalidTransition, JobNotFound) as error:
                raised.append(type(error))
        return raised

    assert with_engine(database_url, body) == [InvalidTransition, JobNotFound]


def test_a_claim_cancels_a_running_job_whose_cancel_was_requested(database_url: str) -> None:
    """Its worker went away before it saw the request: nobody else would stop it."""

    async def body(engine: AsyncEngine) -> tuple[Job | None, list[Job], Job | None]:
        async with engine.begin() as conn:
            await new(conn)
            held = await claim_job(conn, worker_id="w1")
        assert held is not None
        async with engine.begin() as conn:
            await cancel_job(conn, held.id)
            await expire_lease(conn, held.id)
        ended: list[Job] = []
        async with engine.begin() as conn:
            claimed = await claim_job(conn, worker_id="w2", failed=ended)
            return claimed, ended, await get_job(conn, held.id)

    claimed, ended, job = with_engine(database_url, body)

    assert claimed is None
    assert [j.status for j in ended] == [S.CANCELLED]
    assert job is not None
    assert (job.status, job.takeovers, job.lease_owner) == (S.CANCELLED, 0, None)


@pytest.mark.parametrize("held", [False, True])
def test_a_claim_fails_a_job_past_its_deadline_with_an_error_step(
    database_url: str, held: bool
) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job | None, Job | None, list[Any]]:
        async with engine.begin() as conn:
            job = await new(conn)
        if held:
            async with engine.begin() as conn:
                claimed = await claim_job(conn, worker_id="w1")
                assert claimed is not None
                await expire_lease(conn, job.id)
        async with engine.begin() as conn:
            await past_deadline(conn, job)
        async with engine.begin() as conn:
            claimed = await claim_job(conn, worker_id="w2")
            return claimed, await get_job(conn, job.id), await read_steps(conn, job.id)

    claimed, job, steps = with_engine(database_url, body)

    assert claimed is None
    assert job is not None
    assert (job.status, job.error) == (S.FAILED, DEADLINE_ERROR)
    assert [(s.kind, s.payload) for s in steps] == [(StepKind.ERROR, {"message": DEADLINE_ERROR})]


def test_waiting_jobs_past_their_deadline_are_failed_and_no_others(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[Job], dict[str, JobStatus]]:
        async with engine.begin() as conn:
            parked = await new(conn)
            await put(conn, parked, status=S.NEEDS_APPROVAL.value)
            await past_deadline(conn, parked)
            on_time = await new(conn)
            await put(conn, on_time, status=S.NEEDS_APPROVAL.value)
            await new(conn)
            running = await claim_job(conn, worker_id="w1")
            assert running is not None
            await past_deadline(conn, running)
        async with engine.begin() as conn:
            ended = await fail_overdue(conn)
        statuses = {}
        async with engine.connect() as conn:
            for name, job in (("parked", parked), ("on_time", on_time), ("running", running)):
                found = await get_job(conn, job.id)
                assert found is not None
                statuses[name] = found.status
            steps = await read_steps(conn, parked.id)
        assert [(s.kind, s.payload) for s in steps] == [
            (StepKind.ERROR, {"message": DEADLINE_ERROR})
        ]
        return ended, statuses

    ended, statuses = with_engine(database_url, body)

    assert len(ended) == 1
    assert statuses == {"parked": S.FAILED, "on_time": S.NEEDS_APPROVAL, "running": S.RUNNING}


def test_the_workers_timer_fails_a_parked_job_within_seconds_of_its_deadline(
    database_url: str,
) -> None:
    async def body(engine: AsyncEngine) -> float:
        async with engine.begin() as conn:
            job = await new(conn, deadline_in=timedelta(seconds=1))
            await put(conn, job, status=S.NEEDS_APPROVAL.value)
        stop = asyncio.Event()
        timer = asyncio.create_task(keep_failing_overdue(engine, stop, interval=0.2))
        try:
            # Bounded, so a timer that never fails the job fails the test.
            for _ in range(400):
                async with engine.connect() as conn:
                    found = await get_job(conn, job.id)
                    now = await database_now(conn)
                assert found is not None
                if found.status is S.FAILED:
                    return (now - job.deadline).total_seconds()
                await asyncio.sleep(0.05)
            raise AssertionError("the timer never failed the job")
        finally:
            stop.set()
            await timer

    late = with_engine(database_url, body)

    assert late < 5


def test_the_deadline_query_uses_the_open_deadline_index(database_url: str) -> None:
    """Every worker runs it every second: finished jobs must cost it nothing.

    Planned with sequential scans off, because the planner prefers them on a
    table this small.
    """
    sql = str(
        overdue_statement().compile(
            dialect=postgresql.asyncpg.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )

    async def body() -> str:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            await conn.execute("SET enable_seqscan = off")
            plan = await conn.fetch(f"EXPLAIN {sql}")
        finally:
            await conn.close()
        return "\n".join(row[0] for row in plan)

    plan = asyncio.run(body())

    assert "jobs_open_deadline" in plan
    assert "Seq Scan" not in plan
    assert "LockRows" in plan


def test_a_running_job_whose_lease_is_over_is_cancelled_at_once(database_url: str) -> None:
    """Its worker is gone; no claim has to come along first."""

    async def body(engine: AsyncEngine) -> tuple[Job, bool, Job, bool]:
        async with engine.begin() as conn:
            await new(conn)
            held = await claim_job(conn, worker_id="w1")
            assert held is not None
            await expire_lease(conn, held.id)
        async with engine.begin() as conn:
            first, done = await cancel_job(conn, held.id)
        async with engine.begin() as conn:
            again, done_again = await cancel_job(conn, held.id)
        return first, done, again, done_again

    first, done, again, done_again = with_engine(database_url, body)

    assert (first.status, done, first.lease_owner) == (S.CANCELLED, True, None)
    # A second cancel finds it cancelled and says so.
    assert (again.status, done_again) == (S.CANCELLED, True)


def test_a_job_parked_while_its_cancel_is_requested_is_cancelled(database_url: str) -> None:
    """The worker parks it for a person before it saw the request: nobody else would stop it."""

    async def body(engine: AsyncEngine) -> Job:
        async with engine.begin() as conn:
            await new(conn)
            held_job = await claim_job(conn, worker_id="w1")
        assert held_job is not None
        async with engine.begin() as conn:
            await cancel_job(conn, held_job.id)
        question = await ask(engine, held_job.id, held_job.lease_epoch)
        return await held.leave_running(
            engine,
            job_id=held_job.id,
            lease_epoch=held_job.lease_epoch,
            new_status=S.NEEDS_APPROVAL,
            question=question,
        )

    assert with_engine(database_url, body).status is S.CANCELLED


def test_the_timer_fails_a_running_job_past_its_deadline_whose_lease_is_over(
    database_url: str,
) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[Job], Job | None]:
        async with engine.begin() as conn:
            await new(conn)
            orphan = await claim_job(conn, worker_id="w1")
            assert orphan is not None
            await expire_lease(conn, orphan.id)
            await past_deadline(conn, orphan)
        async with engine.begin() as conn:
            ended = await fail_overdue(conn)
        async with engine.connect() as conn:
            return ended, await get_job(conn, orphan.id)

    ended, job = with_engine(database_url, body)

    assert [j.status for j in ended] == [S.FAILED]
    assert job is not None
    assert (job.status, job.error, job.lease_owner) == (S.FAILED, DEADLINE_ERROR, None)
