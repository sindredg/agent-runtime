"""The park writes the row a notification is sent from (ADR 0029).

One statement parks the job, writes its `status_changed` step and its row in
`notifications`. A park that turns into a cancel writes no row, and a stale
holder writes nothing at all.
"""

import subprocess
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store import held
from runtime.store.held import LeaseLost, Question, QuestionReason
from runtime.store.jobs import Job, cancel_job, claim_job, get_job
from runtime.store.schema import notifications
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind, read_steps

from .conftest import (
    Connection,
    alembic,
    ask,
    database_now,
    expire_lease,
    new_job,
    on,
    with_engine,
)
from .test_migrations import current_revision

S = JobStatus
R = QuestionReason


async def held_job(engine: AsyncEngine) -> Job:
    async with engine.begin() as conn:
        await new_job(conn)
        job = await claim_job(conn, worker_id="w1", lease=timedelta(minutes=5))
    assert job is not None
    return job


async def park(engine: AsyncEngine, job: Job, question: Question) -> Job:
    return await held.leave_running(
        engine,
        job_id=job.id,
        lease_epoch=job.lease_epoch,
        new_status=S.NEEDS_APPROVAL,
        question=question,
    )


async def rows(engine: AsyncEngine, job_id: UUID) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        found = await conn.execute(
            select(notifications)
            .where(notifications.c.job_id == job_id)
            .order_by(notifications.c.question)
        )
        return [dict(row._mapping) for row in found]


@pytest.mark.parametrize("reason", list(R))
def test_a_park_writes_the_row_with_the_question_and_its_reason(
    database_url: str, reason: QuestionReason
) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, list[dict[str, Any]], datetime, datetime]:
        job = await held_job(engine)
        question = await ask(engine, job.id, job.lease_epoch, reason)
        async with engine.connect() as conn:
            before = await database_now(conn)
        parked = await park(engine, job, question)
        async with engine.connect() as conn:
            after = await database_now(conn)
        return parked, await rows(engine, job.id), before, after

    parked, found, before, after = with_engine(database_url, body)
    assert parked.status is S.NEEDS_APPROVAL
    (row,) = found
    assert {k: v for k, v in row.items() if k != "next_try"} == {
        "job_id": parked.id,
        "question": 1,
        "reason": reason.value,
        "attempts": 0,
        "delivered_at": None,
        "closed_at": None,
    }
    # Due at once, by the database clock.
    assert before <= row["next_try"] <= after


def test_a_park_that_turns_into_a_cancel_writes_no_row(database_url: str) -> None:
    """Nobody needs to be asked about a job that is cancelled."""

    async def body(engine: AsyncEngine) -> tuple[Job, list[dict[str, Any]]]:
        job = await held_job(engine)
        question = await ask(engine, job.id, job.lease_epoch)
        async with engine.begin() as conn:
            await cancel_job(conn, job.id)
        left = await park(engine, job, question)
        return left, await rows(engine, job.id)

    left, found = with_engine(database_url, body)
    assert left.status is S.CANCELLED
    assert found == []


def test_a_stale_holder_writes_no_row(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job | None, list[dict[str, Any]]]:
        job = await held_job(engine)
        question = await ask(engine, job.id, job.lease_epoch)
        async with engine.begin() as conn:
            await expire_lease(conn, job.id)
            assert await claim_job(conn, worker_id="w2") is not None
        with pytest.raises(LeaseLost):
            await park(engine, job, question)
        async with engine.connect() as conn:
            return await get_job(conn, job.id), await rows(engine, job.id)

    job, found = with_engine(database_url, body)
    assert job is not None
    assert job.status is S.RUNNING
    assert found == []


@pytest.mark.parametrize(
    "which", ["no such step", "another job's step", "the park's own step", "an older question"]
)
def test_a_question_that_is_not_the_jobs_last_question_undoes_the_park(
    database_url: str, which: str
) -> None:
    """The row must name the question the job waits on. The park's own
    `status_changed` step would satisfy the key to `steps`, which is checked
    at the end of the statement. The park, its step and its row go in
    together or not at all."""

    async def body(engine: AsyncEngine) -> tuple[Job | None, list[Any], list[dict[str, Any]]]:
        job = await held_job(engine)
        last = (await ask(engine, job.id, job.lease_epoch)).seq
        seq = {"no such step": 7, "the park's own step": last + 1, "an older question": 1}.get(
            which, 0
        )
        if which == "another job's step":
            other = await held_job(engine)
            await ask(engine, other.id, other.lease_epoch)
            seq = (await ask(engine, other.id, other.lease_epoch)).seq
        if which == "an older question":
            last = (await ask(engine, job.id, job.lease_epoch)).seq
        with pytest.raises(IntegrityError, match='column "reason"'):
            await park(engine, job, Question(seq, R.BEFORE_RUN))
        async with engine.connect() as conn:
            return (
                await get_job(conn, job.id),
                await read_steps(conn, job.id),
                await rows(engine, job.id),
            )

    job, steps, found = with_engine(database_url, body)
    assert job is not None
    assert (job.status, job.lease_owner) == (S.RUNNING, "w1")
    # Only the questions the test wrote.
    assert {step.kind for step in steps} == {StepKind.APPROVAL_REQUESTED}
    assert found == []


def test_a_row_names_a_step_of_its_job(database_url: str) -> None:
    """For any writer, not only the park: the key to `steps`."""

    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(ADD_JOB)
        await conn.execute(
            "INSERT INTO notifications (job_id, question, reason) VALUES ($1, 1, 'before_run')",
            job_id,
        )

    with pytest.raises(asyncpg.ForeignKeyViolationError, match="notifications_question_fkey"):
        on(database_url, body)


def test_a_park_on_a_question_that_has_its_row_leaves_the_row_as_it_is(
    database_url: str,
) -> None:
    """A worker that parked the job, was answered nothing, and parks it again
    must not reset the tries of the row that is being sent."""

    async def body(engine: AsyncEngine) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        job = await held_job(engine)
        question = await ask(engine, job.id, job.lease_epoch)
        await park(engine, job, question)
        async with engine.begin() as conn:
            await conn.execute(
                update(notifications)
                .where(notifications.c.job_id == job.id)
                .values(attempts=3, next_try=text("now() + interval '1 hour'"))
            )
        first = await rows(engine, job.id)
        # As if the worker had stopped after asking and before parking.
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET status = 'running', lease_owner = 'gone',"
                    " lease_expires_at = now() WHERE id = :id"
                ),
                {"id": job.id},
            )
            await expire_lease(conn, job.id)
            again = await claim_job(conn, worker_id="w2")
        assert again is not None
        await park(engine, again, Question(question.seq, R.INTERRUPTED))
        return first, await rows(engine, job.id)

    first, second = with_engine(database_url, body)
    assert first == second
    assert second[0]["attempts"] == 3
    assert second[0]["reason"] == "before_run"


@pytest.mark.parametrize(
    ("status", "question", "message"),
    [
        (S.NEEDS_APPROVAL, None, "a park takes the question"),
        (S.QUEUED, Question(1, R.BEFORE_RUN), "a park takes the question"),
        (S.CANCELLED, Question(1, R.BEFORE_RUN), "a park takes the question"),
        (S.NEEDS_APPROVAL, Question(0, R.BEFORE_RUN), "not a question"),
        (S.NEEDS_APPROVAL, Question(1, "before_run"), "not a question"),  # type: ignore[arg-type]
    ],
)
def test_a_park_and_only_a_park_takes_a_question(
    database_url: str, status: JobStatus, question: Question | None, message: str
) -> None:
    async def body(engine: AsyncEngine) -> None:
        job = await held_job(engine)
        await held.leave_running(
            engine, job_id=job.id, lease_epoch=1, new_status=status, question=question
        )

    with pytest.raises(ValueError, match=message):
        with_engine(database_url, body)


# The table's own rules, for any writer


ADD_JOB = (
    "INSERT INTO jobs (spec, token_budget, deadline)"
    " VALUES ('{}', 1000, now() + interval '1 hour') RETURNING id"
)
ADD_QUESTION = (
    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
    " VALUES ($1, 1, 'approval_requested', '{}', 1)"
)


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            "INSERT INTO notifications (job_id, question, reason) VALUES ($1, 1, 'because')",
            "notifications_reason_valid",
        ),
        (
            "UPDATE notifications SET attempts = -1 WHERE job_id = $1",
            "notifications_attempts_not_negative",
        ),
        (
            "UPDATE notifications SET delivered_at = now() WHERE job_id = $1",
            "notifications_delivered_is_closed",
        ),
    ],
)
def test_the_table_refuses_a_row_that_breaks_its_rules(
    database_url: str, statement: str, constraint: str
) -> None:
    async def body(conn: Connection) -> None:
        job_id = await conn.fetchval(ADD_JOB)
        await conn.execute(ADD_QUESTION, job_id)
        if statement.startswith("UPDATE"):
            await conn.execute(
                "INSERT INTO notifications (job_id, question, reason) VALUES ($1, 1, 'before_run')",
                job_id,
            )
        await conn.execute(statement, job_id)

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        on(database_url, body)


def test_the_table_and_the_kind_arrive_with_their_migration(empty_database_url: str) -> None:
    url = empty_database_url

    async def add(conn: Connection) -> None:
        job_id = await conn.fetchval(ADD_JOB)
        await conn.execute(ADD_QUESTION, job_id)
        await conn.execute(
            "INSERT INTO notifications (job_id, question, reason) VALUES ($1, 1, 'before_run')",
            job_id,
        )
        await conn.execute(
            "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
            " VALUES ($1, 2, 'notification_sent', '{}', 1)",
            job_id,
        )

    alembic(url, "upgrade", "0010")
    with pytest.raises(asyncpg.UndefinedTableError):
        on(url, add)
    alembic(url, "upgrade", "0011")
    on(url, add)

    # A log that holds the kind is not cut to fit the older schema, and the
    # table stays: the downgrade is one transaction.
    with pytest.raises(subprocess.CalledProcessError):
        alembic(url, "downgrade", "0010")
    assert on(url, current_revision) == "0011"
    assert on(url, lambda c: c.fetchval("SELECT count(*) FROM notifications")) == 1


def test_a_notification_sent_step_is_stored_like_any_other(database_url: str) -> None:
    """The kind is in the schema and the chain covers it. Task of the sender:
    write it only while the job waits on that question."""

    async def body(engine: AsyncEngine) -> list[StepKind]:
        job = await held_job(engine)
        question = await ask(engine, job.id, job.lease_epoch)
        await park(engine, job, question)
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
                    " VALUES (:id, 3, 'notification_sent', '{\"question\": 1}', 1)"
                ),
                {"id": job.id},
            )
            return [step.kind for step in await read_steps(conn, job.id)]

    assert with_engine(database_url, body) == [
        StepKind.APPROVAL_REQUESTED,
        StepKind.STATUS_CHANGED,
        StepKind.NOTIFICATION_SENT,
    ]
