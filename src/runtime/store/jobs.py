"""Jobs: create, read, claim, and change status.

Every function takes a connection and leaves the transaction to the caller,
so several calls can share one transaction:

    async with engine.begin() as conn:
        job = await claim_job(conn, worker_id="worker-1")
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Row, Update, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from runtime.store.payload import check_json
from runtime.store.schema import jobs
from runtime.store.status import JobStatus, sources_of

DEFAULT_LEASE = timedelta(seconds=30)


@dataclass(frozen=True, slots=True)
class Job:
    id: UUID
    status: JobStatus
    spec: dict[str, Any]
    token_budget: int
    tokens_used: int
    deadline: datetime
    not_before: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_epoch: int
    cancel_requested: bool
    error: str | None
    created_at: datetime


class InvalidTransition(Exception):
    """The job does not exist or its status may not move to the requested one."""

    def __init__(self, job_id: UUID, new_status: JobStatus) -> None:
        super().__init__(f"job {job_id} is missing or cannot move to {new_status.value}")
        self.job_id = job_id
        self.new_status = new_status


def _job(row: Row[Any]) -> Job:
    fields = dict(row._mapping)
    fields["status"] = JobStatus(fields["status"])
    return Job(**fields)


async def create_job(
    conn: AsyncConnection, *, spec: dict[str, Any], token_budget: int, deadline: datetime
) -> Job:
    """Insert a job in status `queued`. Raises InvalidPayload for a spec jsonb rejects."""
    check_json(spec)
    result = await conn.execute(
        insert(jobs)
        .values(spec=spec, token_budget=token_budget, deadline=deadline)
        .returning(*jobs.c)
    )
    return _job(result.one())


async def get_job(conn: AsyncConnection, job_id: UUID) -> Job | None:
    result = await conn.execute(select(jobs).where(jobs.c.id == job_id))
    row = result.one_or_none()
    return None if row is None else _job(row)


def claim_statement(worker_id: str, lease: timedelta) -> Update:
    """The one statement that is a claim. Separate so a test can inspect its plan."""
    oldest_queued = (
        select(jobs.c.id)
        # literal_execute writes 'queued' into the SQL instead of sending it
        # as a parameter. The planner can only match the partial claim index
        # when it sees the constant.
        .where(jobs.c.status == literal(JobStatus.QUEUED.value, literal_execute=True))
        .order_by(jobs.c.created_at, jobs.c.id)
        .limit(1)
        # FOR NO KEY UPDATE locks the chosen row until commit. It is the lock
        # an UPDATE of non-key columns takes anyway, and unlike FOR UPDATE it
        # does not conflict with the lock a step insert takes on its job row.
        # SKIP LOCKED makes another worker pass over a locked row and take the
        # next one, instead of waiting for a job it can no longer have.
        .with_for_update(skip_locked=True, key_share=True)
        .scalar_subquery()
    )
    return (
        update(jobs)
        .where(jobs.c.id == oldest_queued)
        .values(
            status=JobStatus.RUNNING,
            lease_owner=worker_id,
            # The database clock, never the worker's. clock_timestamp() is the
            # time the statement runs; now() is the time the transaction
            # began, which would shorten the lease by whatever ran before.
            lease_expires_at=func.clock_timestamp() + lease,
            lease_epoch=jobs.c.lease_epoch + 1,
        )
        .returning(*jobs.c)
    )


async def claim_job(
    conn: AsyncConnection, *, worker_id: str, lease: timedelta = DEFAULT_LEASE
) -> Job | None:
    """Claim the oldest queued job for `worker_id`, or return None if there is none.

    The claim moves the job to `running`, sets the lease, and increments
    `lease_epoch`, all in one statement. The claim is final when the caller's
    transaction commits.

    Only `queued` jobs are claimed. Jobs that are backing off or whose lease
    expired are not picked up yet.
    """
    if lease <= timedelta(0):
        raise ValueError("lease must be positive")
    result = await conn.execute(claim_statement(worker_id, lease))
    row = result.one_or_none()
    return None if row is None else _job(row)


async def add_tokens_used(conn: AsyncConnection, job_id: UUID, tokens: int) -> None:
    """Add to the job's token count. Call it in the transaction that records the usage."""
    if tokens < 0:
        raise ValueError("tokens must not be negative")
    await conn.execute(
        update(jobs).where(jobs.c.id == job_id).values(tokens_used=jobs.c.tokens_used + tokens)
    )


async def update_status(
    conn: AsyncConnection, job_id: UUID, new_status: JobStatus, *, error: str | None = None
) -> Job:
    """Move a job to `new_status` and return it. Clears the lease.

    `error` replaces the stored error; leaving it out clears it.

    The move is checked against the transition table inside the UPDATE
    itself, so a job whose status changed in the meantime is not overwritten.
    Raises InvalidTransition when no row was updated. A job becomes `running`
    only through `claim_job`.
    """
    if new_status is JobStatus.RUNNING:
        raise ValueError("a job becomes running through claim_job, not update_status")
    result = await conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id, jobs.c.status.in_(sources_of(new_status)))
        .values(status=new_status, error=error, lease_owner=None, lease_expires_at=None)
        .returning(*jobs.c)
    )
    row = result.one_or_none()
    if row is None:
        raise InvalidTransition(job_id, new_status)
    return _job(row)
