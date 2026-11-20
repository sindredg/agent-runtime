"""A claim and a cancel read the database clock once per statement (issue 127).

Each decides once per row whether a lease or a deadline is over, and status,
both lease columns, the epoch, the takeover count and the error follow that
one decision. A statement that read the clock in each column could see a
lease end or a deadline pass between two reads. It then wrote a row that
disagrees with itself, such as `running` with no lease, or failed on the
lease CHECK.

A stand-in `clock_timestamp()` in its own schema, ahead of `pg_catalog` on
the search path, returns a fixed time plus one microsecond per call. The
sweeps put the end of the lease, or the deadline, after each read in turn.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy import make_url, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from runtime.store.jobs import (
    DEADLINE_ERROR,
    cancel_job,
    claim_job,
    claim_statement,
    create_job,
    get_job,
)
from runtime.store.schema import jobs
from runtime.store.status import JobStatus

from .conftest import as_owner, database_now, with_engine

# The stand-in's time: read n returns BASE plus n microseconds.
BASE = datetime(2020, 1, 1, tzinfo=UTC)
TICK = timedelta(microseconds=1)
# The first and, if the statement reads once, only reading.
FIRST_READ = BASE + TICK
LEASE = timedelta(seconds=30)
# More offsets than the statements have clock_timestamp() calls in their text.
CANCEL_OFFSETS = range(-1, 6)
CLAIM_OFFSETS = range(-1, 18)


@pytest.fixture
def clock_url(database_url: str) -> Iterator[str]:
    """`database_url`, with the stand-in clock installed in its database."""
    role = make_url(database_url).username

    async def install(conn: asyncpg.Connection[Any]) -> None:
        await conn.execute(
            f"""
            CREATE SCHEMA stand_in;
            CREATE SEQUENCE stand_in.reads;
            CREATE FUNCTION stand_in.clock_timestamp() RETURNS timestamptz
                VOLATILE LANGUAGE sql
                AS $$ SELECT timestamptz '{BASE.isoformat()}'
                    + nextval('stand_in.reads') * interval '1 microsecond' $$;
            GRANT USAGE ON SCHEMA stand_in TO {role};
            GRANT USAGE, SELECT, UPDATE ON SEQUENCE stand_in.reads TO {role};
            """
        )

    async def run() -> None:
        conn = await asyncpg.connect(as_owner(database_url), timeout=5)
        try:
            await install(conn)
        finally:
            await conn.close()

    asyncio.run(run())
    yield database_url


async def use_stand_in(conn: AsyncConnection) -> None:
    """From here to the end of the transaction, `clock_timestamp()` is the stand-in, at zero."""
    await conn.execute(text("SET LOCAL search_path = stand_in, pg_catalog, public"))
    await conn.execute(text("SELECT setval('stand_in.reads', 1, false)"))


async def reads(conn: AsyncConnection) -> int:
    """How often the stand-in was read since `use_stand_in`."""
    row = (await conn.execute(text("SELECT last_value, is_called FROM stand_in.reads"))).one()
    return int(row.last_value) if row.is_called else 0


async def set_row(conn: AsyncConnection, job_id: UUID, **values: object) -> None:
    """Write the job's columns with literal values, without reading any clock."""
    await conn.execute(update(jobs).where(jobs.c.id == job_id).values(**values))


# Cancel


@pytest.mark.parametrize("offset", CANCEL_OFFSETS)
def test_a_cancel_decides_once_whether_the_lease_is_over(clock_url: str, offset: int) -> None:
    """The lease ends `offset` reads into the statement: ended, or kept whole.

    Before the fix, offset 1 left the job `running` with no lease, and offset 2
    failed on the lease CHECK.
    """
    lease_end = BASE + offset * TICK

    async def body(engine: AsyncEngine) -> tuple[Any, bool, int]:
        async with engine.begin() as conn:
            job = await create_job(
                conn, spec={"task": "hello"}, token_budget=1000, deadline=BASE + timedelta(days=1)
            )
            await set_row(
                conn,
                job.id,
                status=JobStatus.RUNNING.value,
                lease_owner="w1",
                lease_expires_at=lease_end,
                lease_epoch=1,
            )
        async with engine.begin() as conn:
            await use_stand_in(conn)
            cancelled, done = await cancel_job(conn, job.id)
            return cancelled, done, await reads(conn)

    job, done, read_count = with_engine(clock_url, body)

    if lease_end < FIRST_READ:
        assert (job.status, done) == (JobStatus.CANCELLED, True)
        assert (job.lease_owner, job.lease_expires_at) == (None, None)
    else:
        assert (job.status, done) == (JobStatus.RUNNING, False)
        assert (job.lease_owner, job.lease_expires_at) == ("w1", lease_end)
    assert job.cancel_requested
    assert job.lease_epoch == 1
    assert read_count == 1


def test_a_cancel_that_waited_for_a_renewal_keeps_its_reading(database_url: str) -> None:
    """A cancel blocked on the row decides with the reading it took before it waited.

    The worker renews while the cancel waits for the row, and the renewed
    lease runs out before the worker commits. The cancel's reading is older
    than the renewal, so it sees the lease as live: it asks the worker to stop
    and leaves status and lease as they are. An old reading can only make a
    lease look live, never over. The next claim ends the job.
    """

    async def body(engine: AsyncEngine) -> tuple[Any, bool, datetime, Any]:
        async with engine.begin() as conn:
            job = await create_job(
                conn,
                spec={"task": "hello"},
                token_budget=1000,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
            claimed = await claim_job(conn, worker_id="w1", lease=LEASE)
            assert claimed is not None

        async with engine.connect() as holder, engine.connect() as canceller:
            await holder.begin()
            renewed = (
                await holder.execute(
                    text(
                        "UPDATE jobs SET lease_expires_at = clock_timestamp()"
                        " + interval '200 milliseconds' WHERE id = :id"
                        " RETURNING lease_expires_at"
                    ),
                    {"id": job.id},
                )
            ).scalar_one()

            async def cancel() -> tuple[Any, bool]:
                async with canceller.begin():
                    return await cancel_job(canceller, job.id)

            cancelling = asyncio.create_task(cancel())
            await wait_for_a_lock_wait(holder)
            # The renewed lease runs out while the cancel waits.
            behind = renewed - await database_now(holder)
            await asyncio.sleep(max(behind.total_seconds(), 0) + 0.1)
            assert await database_now(holder) > renewed
            await holder.commit()
            cancelled, done = await asyncio.wait_for(cancelling, timeout=5)

        async with engine.begin() as conn:
            ended = await claim_job(conn, worker_id="w2", lease=LEASE)
            assert ended is None
            after = await get_job(conn, job.id)
        return cancelled, done, renewed, after

    cancelled, done, renewed, after = with_engine(database_url, body)

    assert (cancelled.status, done) == (JobStatus.RUNNING, False)
    assert (cancelled.lease_owner, cancelled.lease_expires_at) == ("w1", renewed)
    assert cancelled.cancel_requested
    assert after.status is JobStatus.CANCELLED
    assert (after.lease_owner, after.lease_expires_at) == (None, None)


async def wait_for_a_lock_wait(conn: AsyncConnection) -> None:
    """Return once another session of this database waits for a row lock."""
    for _ in range(100):
        waiting = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        ).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the cancel never waited for the row")


# Claim


@pytest.mark.parametrize("offset", CLAIM_OFFSETS)
@pytest.mark.parametrize("takeover", [False, True], ids=["queued", "takeover"])
def test_a_claim_decides_once_whether_the_deadline_passed(
    clock_url: str, offset: int, takeover: bool
) -> None:
    """The deadline passes `offset` reads into the claim: failed, or handed out whole.

    Handed out means this worker's lease from the same reading, the epoch one
    up, one more takeover for a takeover, and the error untouched. Failed means
    no lease, epoch and takeovers as they were, and the deadline error.
    """
    deadline = BASE + offset * TICK
    epoch, takeovers = (3, 2) if takeover else (0, 0)

    async def body(engine: AsyncEngine) -> tuple[Any, int]:
        async with engine.begin() as conn:
            job = await create_job(
                conn, spec={"task": "hello"}, token_budget=1000, deadline=deadline
            )
            if takeover:
                await set_row(
                    conn,
                    job.id,
                    status=JobStatus.RUNNING.value,
                    lease_owner="w1",
                    lease_expires_at=BASE - timedelta(hours=1),
                    lease_epoch=epoch,
                    takeovers=takeovers,
                )
        async with engine.begin() as conn:
            await use_stand_in(conn)
            row = (await conn.execute(claim_statement("w2", LEASE))).one()
            return row, await reads(conn)

    row, read_count = with_engine(clock_url, body)

    if deadline <= FIRST_READ:
        assert row.status == JobStatus.FAILED.value
        assert (row.lease_owner, row.lease_expires_at) == (None, None)
        assert (row.lease_epoch, row.takeovers) == (epoch, takeovers)
        assert row.error == DEADLINE_ERROR
    else:
        assert row.status == JobStatus.RUNNING.value
        assert (row.lease_owner, row.lease_expires_at) == ("w2", FIRST_READ + LEASE)
        assert (row.lease_epoch, row.takeovers) == (epoch + 1, takeovers + int(takeover))
        assert row.error is None
    assert read_count == 1
