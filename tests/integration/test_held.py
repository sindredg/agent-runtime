"""A worker's writes to the job it holds, against a real Postgres.

Every such write is fenced: it goes through only while the job is `running`
under the writer's own lease epoch. The tests use two connections where the
order of events between two workers matters.
"""

import asyncio
from collections.abc import Callable, Coroutine
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.util import await_only

from runtime.store import held
from runtime.store.engine import sqlalchemy_url
from runtime.store.held import LeaseLost
from runtime.store.jobs import Job, claim_job, get_job, job_from_row
from runtime.store.payload import InvalidPayload
from runtime.store.schema import jobs
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepConflict, StepKind, read_steps

from .conftest import add_step, expire_lease, new_job, with_engine

S = JobStatus

type Write = Callable[[AsyncEngine, UUID, int], Coroutine[Any, Any, Any]]


async def write_step(engine: AsyncEngine, job_id: UUID, epoch: int) -> Step:
    return await held.append_step(
        engine,
        job_id=job_id,
        lease_epoch=epoch,
        seq=1,
        kind=StepKind.MODEL_RESPONSE,
        payload={"n": 1},
        tokens=7,
    )


async def renew(engine: AsyncEngine, job_id: UUID, epoch: int) -> datetime:
    return await held.renew_lease(
        engine, job_id=job_id, lease_epoch=epoch, lease=timedelta(hours=1)
    )


FAILURE = "the model declined the request"


async def fail(engine: AsyncEngine, job_id: UUID, epoch: int) -> Step:
    return await held.fail(engine, job_id=job_id, lease_epoch=epoch, error=FAILURE)


def leave(new_status: JobStatus) -> Write:
    if new_status is S.FAILED:
        # A job is failed together with its `error` step, by its own write.
        return fail

    async def write(engine: AsyncEngine, job_id: UUID, epoch: int) -> Job:
        return await held.leave_running(
            engine, job_id=job_id, lease_epoch=epoch, new_status=new_status
        )

    return write


# Every kind of write a worker makes to its job.
WRITES = {
    "step": write_step,
    "renewal": renew,
    "finish": leave(S.SUCCEEDED),
    "failure": fail,
    "release": leave(S.QUEUED),
}
each_write = pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())


async def held_job(engine: AsyncEngine) -> Job:
    """A job claimed by `w1`, which holds it under epoch 1."""
    async with engine.begin() as conn:
        await new_job(conn)
        job = await claim_job(conn, worker_id="w1", lease=timedelta(seconds=30))
    assert job is not None
    assert job.lease_epoch == 1
    return job


async def take_over(engine: AsyncEngine, job_id: UUID) -> Job:
    """Let the lease run out and have `w2` claim the job: epoch 2."""
    async with engine.begin() as conn:
        await expire_lease(conn, job_id)
        job = await claim_job(conn, worker_id="w2", lease=timedelta(seconds=30))
    assert job is not None
    assert (job.id, job.lease_epoch) == (job_id, 2)
    return job


async def stored(engine: AsyncEngine, job_id: UUID) -> tuple[Job, list[Step]]:
    async with engine.begin() as conn:
        job = await get_job(conn, job_id)
        assert job is not None
        return job, await read_steps(conn, job_id)


# The holder's own writes go through


def test_the_holder_appends_a_step_and_its_tokens_together(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Step, Job, list[Step]]:
        job = await held_job(engine)
        step = await write_step(engine, job.id, 1)
        return step, *await stored(engine, job.id)

    step, job, steps = with_engine(database_url, body)
    assert steps == [step]
    assert (step.seq, step.kind, step.payload, step.lease_epoch) == (
        1,
        StepKind.MODEL_RESPONSE,
        {"n": 1},
        1,
    )
    assert job.tokens_used == 7


def test_a_renewal_runs_the_lease_from_the_database_clock(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[datetime, datetime, datetime, Job]:
        job = await held_job(engine)
        async with engine.begin() as conn:
            before = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
        expires = await renew(engine, job.id, 1)
        async with engine.begin() as conn:
            after = (await conn.execute(text("SELECT clock_timestamp()"))).scalar_one()
        return before, expires, after, (await stored(engine, job.id))[0]

    before, expires, after, job = with_engine(database_url, body)
    assert before + timedelta(hours=1) <= expires <= after + timedelta(hours=1)
    assert job.lease_expires_at == expires
    assert (job.status, job.lease_owner, job.lease_epoch) == (S.RUNNING, "w1", 1)


def test_an_expired_lease_nobody_took_is_still_the_holders(database_url: str) -> None:
    """The fence is the epoch, not the expiry time: until a takeover the job has one writer."""

    async def body(engine: AsyncEngine) -> tuple[Job, list[Step]]:
        job = await held_job(engine)
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
        await write_step(engine, job.id, 1)
        await renew(engine, job.id, 1)
        return await stored(engine, job.id)

    job, steps = with_engine(database_url, body)
    assert [step.seq for step in steps] == [1]
    assert job.lease_expires_at is not None


@pytest.mark.parametrize(
    "new_status", [S.SUCCEEDED, S.CANCELLED, S.NEEDS_APPROVAL, S.WAITING, S.QUEUED]
)
def test_the_holder_moves_its_job_out_of_running(database_url: str, new_status: JobStatus) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, Job]:
        job = await held_job(engine)
        returned = await held.leave_running(
            engine, job_id=job.id, lease_epoch=1, new_status=new_status
        )
        return returned, (await stored(engine, job.id))[0]

    returned, job = with_engine(database_url, body)
    assert returned == job
    assert (job.status, job.error) == (new_status, None)
    # The lease is given up, and the move is neither a claim nor a takeover.
    assert (job.lease_owner, job.lease_expires_at) == (None, None)
    assert (job.lease_epoch, job.takeovers) == (1, 0)


def test_a_released_job_is_claimed_again_without_counting_a_takeover(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> Job | None:
        job = await held_job(engine)
        await leave(S.QUEUED)(engine, job.id, 1)
        async with engine.begin() as conn:
            return await claim_job(conn, worker_id="w2")

    job = with_engine(database_url, body)
    assert job is not None
    assert (job.status, job.lease_owner) == (S.RUNNING, "w2")
    assert (job.lease_epoch, job.takeovers) == (2, 0)


def test_the_holder_fails_its_job_and_writes_the_error_step_together(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Step, Job, list[Step]]:
        job = await held_job(engine)
        await write_step(engine, job.id, 1)
        step = await fail(engine, job.id, 1)
        return step, *await stored(engine, job.id)

    step, job, steps = with_engine(database_url, body)
    # The step takes the next seq by itself, after the one that was there.
    assert [found.seq for found in steps] == [1, 2]
    assert steps[1] == step
    assert (step.kind, step.payload, step.lease_epoch) == (StepKind.ERROR, {"message": FAILURE}, 1)
    assert (job.status, job.error) == (S.FAILED, FAILURE)
    assert (job.lease_owner, job.lease_expires_at) == (None, None)
    assert (job.lease_epoch, job.takeovers, job.tokens_used) == (1, 0, 7)


def test_a_failure_that_cannot_be_stored_is_refused_before_it_is_sent(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, list[Step]]:
        job = await held_job(engine)
        with pytest.raises(InvalidPayload):
            await held.fail(engine, job_id=job.id, lease_epoch=1, error="bad byte \x00")
        return await stored(engine, job.id)

    job, steps = with_engine(database_url, body)
    assert (job.status, job.error, steps) == (S.RUNNING, None, [])


def test_a_failure_that_waits_for_the_job_row_goes_through_when_it_is_free(
    database_url: str,
) -> None:
    async def body(engine: AsyncEngine) -> tuple[bool, Step, Job, list[Step]]:
        job = await held_job(engine)
        async with engine.connect() as other:
            await other.execute(select(jobs.c.id).where(jobs.c.id == job.id).with_for_update())
            waiting = asyncio.create_task(fail(engine, job.id, 1))
            await asyncio.sleep(0.5)
            waited = not waiting.done()
            await other.commit()
            async with asyncio.timeout(10):
                step = await waiting
        return waited, step, *await stored(engine, job.id)

    waited, step, job, steps = with_engine(database_url, body)
    assert waited
    assert steps == [step]
    assert (step.seq, step.kind) == (1, StepKind.ERROR)
    assert (job.status, job.error) == (S.FAILED, FAILURE)


def test_a_failure_whose_seq_was_taken_while_it_waited_changes_nothing(database_url: str) -> None:
    """The failure picks its `seq` from the log as it was when the statement was sent.

    Here another session holds the job row, writes the step with that `seq`
    and commits. No worker does this: the holder writes its steps one after
    another. If it happens, the step is refused, and the status change must
    go with it. A job that is `failed` without its `error` step is the
    state this write exists to rule out.
    """

    async def body(engine: AsyncEngine) -> tuple[bool, Job, list[Step]]:
        job = await held_job(engine)
        async with engine.connect() as other:
            await other.execute(select(jobs.c.id).where(jobs.c.id == job.id).with_for_update())
            await add_step(other, job.id, 1)
            waiting = asyncio.create_task(fail(engine, job.id, 1))
            await asyncio.sleep(0.5)
            waited = not waiting.done()
            await other.commit()
            with pytest.raises(IntegrityError):
                async with asyncio.timeout(10):
                    await waiting
        return waited, *await stored(engine, job.id)

    waited, job, steps = with_engine(database_url, body)
    assert waited
    assert (job.status, job.error, job.lease_owner) == (S.RUNNING, None, "w1")
    assert [(step.seq, step.kind) for step in steps] == [(1, StepKind.MODEL_REQUEST)]


def test_leave_running_does_not_fail_a_job(database_url: str) -> None:
    """`failed` goes through `fail`, so that no job is failed without its `error` step."""

    async def body(engine: AsyncEngine) -> None:
        job = await held_job(engine)
        await held.leave_running(engine, job_id=job.id, lease_epoch=1, new_status=S.FAILED)

    with pytest.raises(ValueError, match=r"held\.fail"):
        with_engine(database_url, body)


@pytest.mark.parametrize("new_status", [S.RUNNING, "paused"])
def test_leave_running_refuses_a_status_running_cannot_move_to(
    database_url: str, new_status: Any
) -> None:
    async def body(engine: AsyncEngine) -> None:
        job = await held_job(engine)
        await held.leave_running(engine, job_id=job.id, lease_epoch=1, new_status=new_status)

    with pytest.raises(ValueError, match="cannot move"):
        with_engine(database_url, body)


# A worker that lost the job is refused


@each_write
def test_a_write_under_an_old_epoch_is_refused_and_changes_nothing(
    database_url: str, write: Write
) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, Job, list[Step]]:
        job = await held_job(engine)
        taken = await take_over(engine, job.id)
        with pytest.raises(LeaseLost) as refused:
            await write(engine, job.id, 1)
        assert (refused.value.job_id, refused.value.lease_epoch) == (job.id, 1)
        return taken, *await stored(engine, job.id)

    taken, job, steps = with_engine(database_url, body)
    # The job is exactly as the takeover left it.
    assert job == taken
    assert steps == []


@each_write
@pytest.mark.parametrize("status", [S.QUEUED, S.NEEDS_APPROVAL, S.FAILED, S.SUCCEEDED])
def test_a_write_to_a_job_that_is_no_longer_running_is_refused(
    database_url: str, write: Write, status: JobStatus
) -> None:
    """The epoch still matches: the job left `running` without being claimed again."""

    async def body(
        engine: AsyncEngine,
    ) -> tuple[tuple[Job, list[Step]], tuple[Job, list[Step]]]:
        job = await held_job(engine)
        await leave(status)(engine, job.id, 1)
        left = await stored(engine, job.id)
        with pytest.raises(LeaseLost):
            await write(engine, job.id, 1)
        return left, await stored(engine, job.id)

    left, found = with_engine(database_url, body)
    assert found == left
    assert left[0].status is status


@each_write
def test_a_write_to_a_job_that_does_not_exist_is_refused(database_url: str, write: Write) -> None:
    async def body(engine: AsyncEngine) -> None:
        await write(engine, uuid4(), 1)

    with pytest.raises(LeaseLost):
        with_engine(database_url, body)


def test_a_seq_conflict_raises_and_takes_the_token_count_back(database_url: str) -> None:
    """The step and its tokens are one statement: when the step fails, so does the count."""

    async def body(engine: AsyncEngine) -> tuple[Job, list[Step]]:
        job = await held_job(engine)
        await write_step(engine, job.id, 1)
        with pytest.raises(StepConflict):
            await write_step(engine, job.id, 1)
        return await stored(engine, job.id)

    job, steps = with_engine(database_url, body)
    assert [step.seq for step in steps] == [1]
    assert job.tokens_used == 7


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"seq": 0}, "seq starts at 1"),
        ({"tokens": -1}, "tokens must not be negative"),
    ],
)
def test_a_step_that_cannot_be_valid_is_refused_before_it_is_sent(
    database_url: str, change: dict[str, int], error: str
) -> None:
    async def body(engine: AsyncEngine) -> None:
        job = await held_job(engine)
        arguments: dict[str, Any] = {"seq": 1, "tokens": 0, **change}
        await held.append_step(
            engine,
            job_id=job.id,
            lease_epoch=1,
            kind=StepKind.ERROR,
            payload={},
            **arguments,
        )

    with pytest.raises(ValueError, match=error):
        with_engine(database_url, body)


# The order of events between two workers


@each_write
def test_a_write_sent_while_a_takeover_is_uncommitted_waits_and_is_refused(
    database_url: str, write: Write
) -> None:
    """First order: the claim is still open when the stale worker's write arrives.

    A write that only compared epochs would read the job as it was before
    the claim and go through. The row lock makes it wait for the claim to
    commit and then look at the job again.
    """

    async def body(engine: AsyncEngine) -> tuple[bool, Job, Job, list[Step]]:
        job = await held_job(engine)
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
        async with engine.connect() as claimer:
            taken = await claim_job(claimer, worker_id="w2", lease=timedelta(seconds=30))
            assert taken is not None
            assert (taken.id, taken.lease_epoch) == (job.id, 2)
            stale = asyncio.create_task(write(engine, job.id, 1))
            await asyncio.sleep(0.5)
            waited = not stale.done()
            await claimer.commit()
            with pytest.raises(LeaseLost):
                async with asyncio.timeout(10):
                    await stale
        return waited, taken, *await stored(engine, job.id)

    waited, taken, job, steps = with_engine(database_url, body)
    assert waited
    assert job == taken
    assert steps == []


def test_a_takeover_right_after_a_write_finds_the_job_unlocked_and_the_step_stored(
    database_url: str,
) -> None:
    """Second order: once a write has returned, nothing of it is still open.

    The claim skips locked rows. It gets the job, and the step is already
    visible to it, so the write left neither a lock nor an open transaction.
    """

    async def body(engine: AsyncEngine) -> tuple[Job, Job | None, list[Step]]:
        job = await held_job(engine)
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
        await write_step(engine, job.id, 1)
        async with engine.begin() as conn:
            taken = await claim_job(conn, worker_id="w2")
            return job, taken, await read_steps(conn, job.id)

    job, taken, steps = with_engine(database_url, body)
    assert taken is not None
    assert (taken.id, taken.lease_epoch, taken.takeovers) == (job.id, 2, 1)
    assert [(step.seq, step.lease_epoch) for step in steps] == [(1, 1)]


@each_write
def test_a_worker_paused_right_after_its_statement_holds_nothing(
    database_url: str, write: Write
) -> None:
    """Second order, the cause: a write commits when its statement completes.

    A worker that sent BEGIN, the statement and COMMIT one by one could be
    paused between the statement and the commit, holding the row lock. The
    hook below runs at exactly that point: the statement has returned and
    the worker has done nothing else yet. A second connection must find the
    row unlocked and already in its final state.
    """

    async def look(other: AsyncEngine, job_id: UUID) -> Job | None:
        async with other.begin() as conn:
            result = await conn.execute(
                select(jobs).where(jobs.c.id == job_id).with_for_update(skip_locked=True)
            )
            row = result.one_or_none()
            return None if row is None else job_from_row(row)

    async def body(engine: AsyncEngine) -> tuple[list[Job | None], Job]:
        job = await held_job(engine)
        other = create_async_engine(sqlalchemy_url(database_url))
        seen: list[Job | None] = []

        def paused(*statement: Any) -> None:
            # Events of the async engine run in a greenlet. await_only lets
            # this plain function wait for a coroutine from inside it.
            seen.append(await_only(look(other, job.id)))

        event.listen(engine.sync_engine, "after_cursor_execute", paused)
        try:
            await write(engine, job.id, 1)
        finally:
            event.remove(engine.sync_engine, "after_cursor_execute", paused)
            await other.dispose()
        return seen, (await stored(engine, job.id))[0]

    seen, job = with_engine(database_url, body)
    # One statement was sent, and when it returned the row was not locked
    # (the look found it) and the write was committed (it found the final row).
    assert seen == [job]
