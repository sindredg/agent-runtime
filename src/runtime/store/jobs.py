"""Jobs: create, read, claim, and change status.

Every function takes a connection and leaves the transaction to the caller,
so several calls can share one transaction:

    async with engine.begin() as conn:
        job = await claim_job(conn, worker_id="worker-1")
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Row, Update, and_, case, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from runtime.store.payload import check_json
from runtime.store.schema import jobs, steps
from runtime.store.status import JobStatus, sources_of
from runtime.store.steps import StepKind

log = logging.getLogger("runtime.store")

DEFAULT_LEASE = timedelta(seconds=30)
# How often a job may be taken over from a worker that went away. The claim
# after that fails the job, so a job runs at most this many times plus one.
TAKEOVER_LIMIT = 5
TAKEOVER_LIMIT_ERROR = (
    f"the job was taken over {TAKEOVER_LIMIT} times after its worker stopped; it is not run again"
)


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
    # Claims that took the job from a worker whose lease had expired.
    takeovers: int
    cancel_requested: bool
    error: str | None
    created_at: datetime


class InvalidTransition(Exception):
    """The job does not exist or its status may not move to the requested one."""

    def __init__(self, job_id: UUID, new_status: JobStatus) -> None:
        super().__init__(f"job {job_id} is missing or cannot move to {new_status.value}")
        self.job_id = job_id
        self.new_status = new_status


def job_from_row(row: Row[Any]) -> Job:
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
    return job_from_row(result.one())


async def get_job(conn: AsyncConnection, job_id: UUID) -> Job | None:
    result = await conn.execute(select(jobs).where(jobs.c.id == job_id))
    row = result.one_or_none()
    return None if row is None else job_from_row(row)


def _literal(value: object) -> Any:
    """A constant written into the SQL instead of sent as a parameter.

    The planner can only match the partial claim index when it sees the
    status as a constant.
    """
    return literal(value, literal_execute=True)


def claim_statement(worker_id: str, lease: timedelta) -> Update:
    """The one statement that is a claim. Separate so a test can inspect its plan.

    It picks the oldest job that is `queued`, or `running` under a lease
    that has expired. The second kind is a takeover. A job that has already
    been taken over TAKEOVER_LIMIT times is not handed out: the same
    statement marks it `failed`.
    """
    queued = _literal(JobStatus.QUEUED.value)
    running = _literal(JobStatus.RUNNING.value)
    oldest_claimable = (
        select(jobs.c.id)
        .where(
            or_(
                jobs.c.status == queued,
                # The database clock decides whether a lease is over. A
                # `running` job without a lease does not match: NULL is not
                # earlier than anything.
                and_(jobs.c.status == running, jobs.c.lease_expires_at < func.clock_timestamp()),
            )
        )
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
    # In SET, a column name is the value before the update.
    takeover = jobs.c.status == running
    at_limit = and_(takeover, jobs.c.takeovers >= TAKEOVER_LIMIT)
    return (
        update(jobs)
        .where(jobs.c.id == oldest_claimable)
        .values(
            status=case((at_limit, JobStatus.FAILED.value), else_=JobStatus.RUNNING.value),
            lease_owner=case((at_limit, None), else_=worker_id),
            # The database clock, never the worker's. clock_timestamp() is the
            # time the statement runs; now() is the time the transaction
            # began, which would shorten the lease by whatever ran before.
            lease_expires_at=case((at_limit, None), else_=func.clock_timestamp() + lease),
            # A claim that fails the job hands it to nobody, so it is not counted.
            lease_epoch=jobs.c.lease_epoch + case((at_limit, 0), else_=1),
            takeovers=jobs.c.takeovers + case((and_(takeover, ~at_limit), 1), else_=0),
            error=case((at_limit, TAKEOVER_LIMIT_ERROR), else_=jobs.c.error),
        )
        .returning(*jobs.c)
    )


async def _record_takeover_limit(conn: AsyncConnection, job: Job) -> None:
    """Append the `error` step for a job the claim just failed.

    The claim's transaction still holds the job row's lock, so no worker
    write can take the same `seq` in between.
    """
    next_seq = (
        select(func.coalesce(func.max(steps.c.seq), 0) + 1)
        .where(steps.c.job_id == job.id)
        .scalar_subquery()
    )
    await conn.execute(
        insert(steps).values(
            job_id=job.id,
            seq=next_seq,
            kind=StepKind.ERROR,
            payload={"message": TAKEOVER_LIMIT_ERROR},
            lease_epoch=job.lease_epoch,
        )
    )
    log.warning(
        "job failed at the takeover limit",
        extra={"job_id": job.id, "takeovers": job.takeovers},
    )


async def claim_job(
    conn: AsyncConnection, *, worker_id: str, lease: timedelta = DEFAULT_LEASE
) -> Job | None:
    """Claim the oldest claimable job for `worker_id`, or return None if there is none.

    Claimable is a `queued` job, or a `running` job whose lease has expired.
    The claim moves the job to `running`, sets the lease, and increments
    `lease_epoch`, all in one statement. Taking a `running` job also
    increments `takeovers`. The claim is final when the caller's transaction
    commits.

    A job that was already taken over TAKEOVER_LIMIT times is marked `failed`
    with an `error` step, in the caller's transaction, and the next claimable
    job is looked for.

    Jobs that are backing off (`waiting`) are not picked up yet.
    """
    if lease <= timedelta(0):
        raise ValueError("lease must be positive")
    while True:
        result = await conn.execute(claim_statement(worker_id, lease))
        row = result.one_or_none()
        if row is None:
            return None
        job = job_from_row(row)
        if job.status is JobStatus.RUNNING:
            return job
        await _record_takeover_limit(conn, job)


async def update_status(
    conn: AsyncConnection, job_id: UUID, new_status: JobStatus, *, error: str | None = None
) -> Job:
    """Move a job that no worker holds to `new_status` and return it.

    `error` replaces the stored error; leaving it out clears it.

    The move is checked against the transition table inside the UPDATE
    itself, so a job whose status changed in the meantime is not overwritten.
    Raises InvalidTransition when no row was updated.

    A `running` job is never moved here. Only the worker that holds it moves
    it, through `runtime.store.held.leave_running`, which checks the lease
    epoch. A job becomes `running` only through `claim_job`.
    """
    if new_status is JobStatus.RUNNING:
        raise ValueError("a job becomes running through claim_job, not update_status")
    sources = sources_of(new_status) - {JobStatus.RUNNING}
    result = await conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id, jobs.c.status.in_(sources))
        .values(status=new_status, error=error)
        .returning(*jobs.c)
    )
    row = result.one_or_none()
    if row is None:
        raise InvalidTransition(job_id, new_status)
    return job_from_row(row)
