"""A cancel and a park for approval are steps in the log (APTS AR-002).

Each writes a `status_changed` step with the status before, the status after,
and the cause, together with the move: in the same statement for the worker
that holds the job, in the same transaction for the API and the claim. A
release to `queued` and a finish write none.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store import held
from runtime.store.chain import verify_job
from runtime.store.held import LeaseLost
from runtime.store.jobs import Job, cancel_job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps

from .conftest import alembic, expire_lease, new_job, on, with_engine

S = JobStatus


def change(before: str, after: str, cause: str) -> dict[str, Any]:
    return {"from": before, "to": after, "cause": cause}


CANCELLED_FROM_RUNNING = change("running", "cancelled", "cancel_requested")
PARKED = change("running", "needs_approval", "approval_requested")


async def held_job(engine: AsyncEngine) -> Job:
    async with engine.begin() as conn:
        await new_job(conn)
        job = await claim_job(conn, worker_id="w1", lease=timedelta(minutes=5))
    assert job is not None
    return job


async def leave(engine: AsyncEngine, job: Job, status: JobStatus) -> Job:
    return await held.leave_running(
        engine, job_id=job.id, lease_epoch=job.lease_epoch, new_status=status
    )


async def cancel(engine: AsyncEngine, job_id: UUID) -> Job:
    async with engine.begin() as conn:
        job, _ = await cancel_job(conn, job_id)
    return job


async def log(engine: AsyncEngine, job_id: UUID) -> tuple[JobStatus, list[Step]]:
    async with engine.begin() as conn:
        job = await get_job(conn, job_id)
        assert job is not None
        steps = await read_steps(conn, job_id)
        check = await verify_job(conn, job_id)
    assert check.mismatches == []
    return job.status, steps


def changes(steps: list[Step]) -> list[tuple[int, dict[str, Any]]]:
    return [(s.seq, s.payload) for s in steps if s.kind is StepKind.STATUS_CHANGED]


def test_a_park_is_written_with_the_move(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, JobStatus, list[Step]]:
        job = await held_job(engine)
        left = await leave(engine, job, S.NEEDS_APPROVAL)
        return left, *await log(engine, job.id)

    left, status, steps = with_engine(database_url, body)
    assert left.status is status is S.NEEDS_APPROVAL
    assert changes(steps) == [(1, PARKED)]
    assert steps[0].lease_epoch == 1


def test_a_park_while_a_cancel_is_requested_is_written_as_the_cancel(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[JobStatus, list[Step]]:
        job = await held_job(engine)
        # Held under a live lease: the API only asks the worker to stop.
        assert (await cancel(engine, job.id)).status is S.RUNNING
        await leave(engine, job, S.NEEDS_APPROVAL)
        return await log(engine, job.id)

    status, steps = with_engine(database_url, body)
    assert status is S.CANCELLED
    assert changes(steps) == [(1, CANCELLED_FROM_RUNNING)]


def test_the_holder_writes_its_cancel(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[JobStatus, list[Step]]:
        job = await held_job(engine)
        await cancel(engine, job.id)
        await leave(engine, job, S.CANCELLED)
        return await log(engine, job.id)

    status, steps = with_engine(database_url, body)
    assert status is S.CANCELLED
    assert changes(steps) == [(1, CANCELLED_FROM_RUNNING)]


@pytest.mark.parametrize("status", [S.QUEUED, S.SUCCEEDED])
def test_a_release_and_a_finish_write_no_step(database_url: str, status: JobStatus) -> None:
    async def body(engine: AsyncEngine) -> list[Step]:
        job = await held_job(engine)
        await leave(engine, job, status)
        return (await log(engine, job.id))[1]

    assert with_engine(database_url, body) == []


def test_a_stale_holder_writes_neither_the_move_nor_the_step(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[JobStatus, list[Step]]:
        job = await held_job(engine)
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
            assert await claim_job(conn, worker_id="w2") is not None
        with pytest.raises(LeaseLost):
            await leave(engine, job, S.NEEDS_APPROVAL)
        return await log(engine, job.id)

    status, steps = with_engine(database_url, body)
    assert (status, steps) == (S.RUNNING, [])


@pytest.mark.parametrize("before", ["queued", "needs_approval", "running with its lease over"])
def test_the_api_writes_the_cancel_of_a_job_nobody_holds(database_url: str, before: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[JobStatus, list[Step], JobStatus]:
        if before == "queued":
            async with engine.begin() as conn:
                job = await new_job(conn)
        else:
            job = await held_job(engine)
            if before == "needs_approval":
                await leave(engine, job, S.NEEDS_APPROVAL)
            else:
                async with engine.begin() as conn:
                    await expire_lease(conn, job.id)
        await cancel(engine, job.id)
        # A second cancel of a cancelled job changes nothing and writes nothing.
        again = await cancel(engine, job.id)
        return *await log(engine, job.id), again.status

    status, steps, again = with_engine(database_url, body)
    previous = before.split()[0]
    assert (status, again) == (S.CANCELLED, S.CANCELLED)
    assert changes(steps)[-1][1] == change(previous, "cancelled", "cancel_requested")
    assert [s.kind for s in steps][-1] is StepKind.STATUS_CHANGED
    assert len(changes(steps)) == (2 if previous == "needs_approval" else 1)


def test_a_claim_writes_the_cancel_it_makes(database_url: str) -> None:
    """A job released with its cancel requested is cancelled by the next claim."""

    async def body(engine: AsyncEngine) -> tuple[JobStatus, list[Step], Job | None]:
        job = await held_job(engine)
        await cancel(engine, job.id)
        await leave(engine, job, S.QUEUED)
        async with engine.begin() as conn:
            claimed = await claim_job(conn, worker_id="w2")
        return *await log(engine, job.id), claimed

    status, steps, claimed = with_engine(database_url, body)
    assert claimed is None
    assert status is S.CANCELLED
    assert changes(steps) == [(1, change("queued", "cancelled", "cancel_requested"))]


def test_the_kind_arrives_with_its_migration(empty_database_url: str) -> None:
    url = empty_database_url

    async def add(conn: Any) -> None:
        job_id = await conn.fetchval(
            "INSERT INTO jobs (spec, token_budget, deadline)"
            " VALUES ('{}', 1000, now() + interval '1 hour') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
            " VALUES ($1, 1, 'status_changed', '{}', 1)",
            job_id,
        )

    alembic(url, "upgrade", "0007")
    with pytest.raises(Exception, match="steps_kind_valid"):
        on(url, add)
    alembic(url, "upgrade", "0008")
    on(url, add)
