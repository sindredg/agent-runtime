"""A worker's writes to the job it holds: steps, lease renewals, status changes, failure.

Each write is fenced. It is one statement that updates the job row, and the
update only matches while the job is `running` under the writer's own
`lease_epoch`. An UPDATE locks its row, so a write and a claim of the same job
cannot overlap: one waits for the other and then sees its result. A write
that changes no row raises LeaseLost. The worker then drops the job.

The statements are sent in autocommit, which means outside a transaction
block. Each commits when it completes. Sent as BEGIN, statement, COMMIT, a
worker could be paused between the statement and the commit while it holds
the row lock, and no other worker could take the job over.

That is why these functions take the engine and not a connection: the caller
cannot wrap them in a transaction of its own.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Executable, Integer, Row, Text, func, insert, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from runtime.store.jobs import Job, job_from_row
from runtime.store.payload import check_json
from runtime.store.schema import jobs, steps
from runtime.store.status import JobStatus, can_move
from runtime.store.steps import Step, StepConflict, StepKind, next_seq, step_from_row

# Postgres reports a broken unique constraint with this SQLSTATE.
_UNIQUE_VIOLATION = "23505"


class LeaseLost(Exception):
    """The job is no longer `running` under this lease epoch. Nothing was written."""

    def __init__(self, job_id: UUID, lease_epoch: int) -> None:
        super().__init__(f"job {job_id} is not held under lease epoch {lease_epoch}")
        self.job_id = job_id
        self.lease_epoch = lease_epoch


def _held(job_id: UUID, lease_epoch: int) -> tuple[ColumnElement[bool], ...]:
    """The fence: the conditions every write puts on the job row."""
    return (
        jobs.c.id == job_id,
        jobs.c.status == JobStatus.RUNNING.value,
        jobs.c.lease_epoch == lease_epoch,
    )


async def _send(
    engine: AsyncEngine, statement: Executable, job_id: UUID, lease_epoch: int
) -> Row[Any]:
    """Run one fenced statement in autocommit and return its one row, or raise LeaseLost."""
    async with engine.connect() as conn:
        # Autocommit: no BEGIN is sent, the statement is its own transaction.
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        row = (await conn.execute(statement)).one_or_none()
    if row is None:
        raise LeaseLost(job_id, lease_epoch)
    return row


async def append_step(
    engine: AsyncEngine,
    *,
    job_id: UUID,
    lease_epoch: int,
    seq: int,
    kind: StepKind,
    payload: dict[str, Any],
    tokens: int = 0,
) -> Step:
    """Append a step and add `tokens` to the job's count, in one statement.

    Raises LeaseLost when the job is not held under `lease_epoch`, and
    StepConflict when the job already has a step with that `seq`. Either
    way neither the step nor the tokens are stored.
    """
    if seq < 1:
        raise ValueError("seq starts at 1")
    if tokens < 0:
        raise ValueError("tokens must not be negative")
    check_json(payload)
    # The update is the fence and the row lock. With no tokens to add it
    # changes no value and is still made, for the lock.
    held = (
        update(jobs)
        .where(*_held(job_id, lease_epoch))
        .values(tokens_used=jobs.c.tokens_used + tokens)
        .returning(jobs.c.id)
        .cte("held")
    )
    # The step is inserted from the rows the update returned: one or none.
    statement = (
        insert(steps)
        .from_select(
            ["job_id", "seq", "kind", "payload", "lease_epoch"],
            select(
                held.c.id,
                literal(seq, Integer),
                literal(kind.value, Text),
                literal(payload, JSONB),
                literal(lease_epoch, Integer),
            ),
        )
        .returning(*steps.c)
    )
    try:
        row = await _send(engine, statement, job_id, lease_epoch)
    except IntegrityError as error:
        # No ON CONFLICT here: the error undoes the whole statement, and with
        # it the token count.
        if getattr(error.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
            raise StepConflict(job_id, seq) from error
        raise
    return step_from_row(row)


async def renew_lease(
    engine: AsyncEngine, *, job_id: UUID, lease_epoch: int, lease: timedelta
) -> datetime:
    """Run the lease for `lease` from now, by the database clock. Returns the new expiry."""
    if lease <= timedelta(0):
        raise ValueError("lease must be positive")
    statement = (
        update(jobs)
        .where(*_held(job_id, lease_epoch))
        .values(lease_expires_at=func.clock_timestamp() + lease)
        .returning(jobs.c.lease_expires_at)
    )
    row = await _send(engine, statement, job_id, lease_epoch)
    expires: datetime = row.lease_expires_at
    return expires


async def leave_running(
    engine: AsyncEngine,
    *,
    job_id: UUID,
    lease_epoch: int,
    new_status: JobStatus,
) -> Job:
    """Move the held job from `running` to `new_status` and give up the lease.

    This is how a worker finishes a job, parks it for an approval, and
    releases it back to `queued`. A stored error is cleared.

    Not to `failed`: that is `fail`, which writes the `error` step with it.
    """
    if new_status is JobStatus.FAILED:
        raise ValueError("a job is failed with held.fail, which also writes its error step")
    if not isinstance(new_status, JobStatus) or not can_move(JobStatus.RUNNING, new_status):
        raise ValueError(f"a running job cannot move to {new_status!r}")
    statement = (
        update(jobs)
        .where(*_held(job_id, lease_epoch))
        .values(status=new_status.value, error=None, lease_owner=None, lease_expires_at=None)
        .returning(*jobs.c)
    )
    return job_from_row(await _send(engine, statement, job_id, lease_epoch))


async def fail(engine: AsyncEngine, *, job_id: UUID, lease_epoch: int, error: str) -> Step:
    """Fail the held job: the `error` step and the move to `failed`, in one statement.

    Returns the step. One statement, so a worker that stops while it fails
    a job leaves both or neither. A job that was still `running` with an
    `error` step in its log would be taken over and run on.

    The step takes the next free `seq` by itself, as the statement saw the
    log when it was sent. The holder writes its steps one after another, so
    nothing takes that `seq` in between. If a step did, while this statement
    waited for the job row, the unique constraint refuses the step with
    IntegrityError and the status change is undone with it.
    """
    payload = {"message": error}
    check_json(payload)
    held = (
        update(jobs)
        .where(*_held(job_id, lease_epoch))
        .values(status=JobStatus.FAILED.value, error=error, lease_owner=None, lease_expires_at=None)
        .returning(jobs.c.id)
        .cte("held")
    )
    statement = (
        insert(steps)
        .from_select(
            ["job_id", "seq", "kind", "payload", "lease_epoch"],
            select(
                held.c.id,
                next_seq(job_id),
                literal(StepKind.ERROR.value, Text),
                literal(payload, JSONB),
                literal(lease_epoch, Integer),
            ),
        )
        .returning(*steps.c)
    )
    return step_from_row(await _send(engine, statement, job_id, lease_epoch))


__all__ = ["LeaseLost", "append_step", "fail", "leave_running", "renew_lease"]
