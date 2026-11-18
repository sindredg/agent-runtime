"""Jobs: create, read, claim, and change status.

Every function takes a connection and leaves the transaction to the caller,
so several calls can share one transaction:

    async with engine.begin() as conn:
        job = await claim_job(conn, worker_id="worker-1")
"""

import logging
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Row,
    Select,
    Update,
    and_,
    case,
    func,
    insert,
    literal,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import ColumnElement, Label

from runtime.store.payload import check_json
from runtime.store.schema import job_columns, jobs, steps
from runtime.store.status import FINAL_STATUSES, JobStatus, sources_of
from runtime.store.steps import StatusCause, StepKind, next_seq

log = logging.getLogger("runtime.store")

DEFAULT_LEASE = timedelta(seconds=30)
# How often a job may be taken over from a worker that went away. The
# takeover after that fails the job instead. Only takeovers are counted and
# limited: a claim after a release or an approval is neither.
TAKEOVER_LIMIT = 5
TAKEOVER_LIMIT_ERROR = (
    f"the job was taken over {TAKEOVER_LIMIT} times after its worker stopped; it is not run again"
)
DEADLINE_ERROR = "the job's deadline passed"
# The statuses of a job that no worker holds and that is not final.
_UNHELD = (JobStatus.QUEUED, JobStatus.WAITING, JobStatus.NEEDS_APPROVAL)


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


class OtherQuestion(Exception):
    """An answer names another question than the one the job waits on (ADR 0012)."""

    def __init__(self, job_id: UUID, question: int, waiting_on: int | None) -> None:
        super().__init__(f"job {job_id} waits on question {waiting_on}, not on {question}")
        self.job_id = job_id
        self.question = question
        # The `seq` of the question the job waits on. None if it asked none.
        self.waiting_on = waiting_on


_JOB_FIELDS = tuple(field.name for field in fields(Job))

# The status a row had before the UPDATE that returns it. Postgres 18 names
# the row before the change `old` in RETURNING.
PREVIOUS_STATUS: Label[str] = literal_column("old.status").label("previous_status")


def job_from_row(row: Row[Any]) -> Job:
    """The job in a row. Columns that are not a field of Job are left out."""
    mapping = row._mapping
    values = {name: mapping[name] for name in _JOB_FIELDS}
    values["status"] = JobStatus(values["status"])
    return Job(**values)


def status_change(previous: str, job: Job, cause: StatusCause) -> dict[str, str]:
    """The payload of a `status_changed` step (APTS AR-002): from, to, and why."""
    return {"from": previous, "to": job.status.value, "cause": cause.value}


async def create_job(
    conn: AsyncConnection, *, spec: dict[str, Any], token_budget: int, deadline: datetime
) -> Job:
    """Insert a job in status `queued`. Raises InvalidPayload for a spec jsonb rejects."""
    check_json(spec)
    result = await conn.execute(
        insert(jobs)
        .values(spec=spec, token_budget=token_budget, deadline=deadline)
        .returning(*job_columns)
    )
    return job_from_row(result.one())


async def get_job(conn: AsyncConnection, job_id: UUID) -> Job | None:
    result = await conn.execute(select(*job_columns).where(jobs.c.id == job_id))
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
    that has expired. The second kind is a takeover. The same statement ends
    a job instead of handing it out, in this order: one whose cancel was
    requested is `cancelled`; one past its deadline is `failed`; a takeover
    of a job that has already been taken over TAKEOVER_LIMIT times is
    `failed`.
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
        # an UPDATE of non-key columns takes anyway. A step insert takes the
        # same lock on its job row to move the chain's head (migration 0007),
        # so a job whose step is being written is passed over. SKIP LOCKED
        # makes another worker pass over a locked row and take the next one,
        # instead of waiting for a job it can no longer have.
        .with_for_update(skip_locked=True, key_share=True)
        .scalar_subquery()
    )
    # In SET, a column name is the value before the update.
    takeover = jobs.c.status == running
    cancelled = jobs.c.cancel_requested
    overdue = and_(~cancelled, jobs.c.deadline <= func.clock_timestamp())
    at_limit = and_(~cancelled, ~overdue, takeover, jobs.c.takeovers >= TAKEOVER_LIMIT)
    ended = or_(cancelled, overdue, at_limit)
    return (
        update(jobs)
        .where(jobs.c.id == oldest_claimable)
        .values(
            status=case(
                (cancelled, JobStatus.CANCELLED.value),
                (or_(overdue, at_limit), JobStatus.FAILED.value),
                else_=JobStatus.RUNNING.value,
            ),
            lease_owner=case((ended, None), else_=worker_id),
            # The database clock, never the worker's. clock_timestamp() is the
            # time the statement runs; now() is the time the transaction
            # began, which would shorten the lease by whatever ran before.
            lease_expires_at=case((ended, None), else_=func.clock_timestamp() + lease),
            # A claim that ends the job hands it to nobody, so it is not
            # counted. A stale worker is fenced off all the same: the job is
            # no longer `running`.
            lease_epoch=jobs.c.lease_epoch + case((ended, 0), else_=1),
            takeovers=jobs.c.takeovers + case((and_(takeover, ~ended), 1), else_=0),
            error=case(
                (overdue, DEADLINE_ERROR),
                (at_limit, TAKEOVER_LIMIT_ERROR),
                (cancelled, None),
                else_=jobs.c.error,
            ),
        )
        .returning(*job_columns, PREVIOUS_STATUS)
    )


async def _append_next_step(
    conn: AsyncConnection, job: Job, kind: StepKind, payload: dict[str, Any]
) -> None:
    """Append a step at the job's next `seq`, for a caller that holds the job row's lock.

    The claim and an answer to an approval both lock the job row first, in
    the same transaction. Until that transaction ends no worker can write a step for
    the job, so the next `seq` cannot be taken in between.
    """
    await conn.execute(
        insert(steps).values(
            job_id=job.id,
            seq=next_seq(job.id),
            kind=kind,
            payload=payload,
            lease_epoch=job.lease_epoch,
        )
    )


async def _record_end(conn: AsyncConnection, job: Job, previous: str) -> None:
    """Record a job the claim ended: a step, and a log line.

    An `error` step for a failure, a `status_changed` step for a cancel.
    """
    if job.status is JobStatus.FAILED:
        await _append_next_step(conn, job, StepKind.ERROR, {"message": job.error})
    else:
        await _append_next_step(
            conn,
            job,
            StepKind.STATUS_CHANGED,
            status_change(previous, job, StatusCause.CANCEL_REQUESTED),
        )
    log.warning(
        "job ended by the claim",
        extra={"job_id": job.id, "status": job.status.value, "error": job.error},
    )


async def claim_job(
    conn: AsyncConnection,
    *,
    worker_id: str,
    lease: timedelta = DEFAULT_LEASE,
    failed: list[Job] | None = None,
) -> Job | None:
    """Claim the oldest claimable job for `worker_id`, or return None if there is none.

    Claimable is a `queued` job, or a `running` job whose lease has expired.
    The claim moves the job to `running`, sets the lease, and increments
    `lease_epoch`, all in one statement. Taking a `running` job also
    increments `takeovers`. The claim is final when the caller's transaction
    commits.

    A job that the claim ends instead (see `claim_statement`) gets an
    `error` step if it failed, in the caller's transaction, and the next
    claimable job is looked for. Such jobs are appended to `failed`, if
    given: no worker runs them again, so the caller is the one to clean up
    after them once its transaction has committed.

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
        await _record_end(conn, job, row.previous_status)
        if failed is not None:
            failed.append(job)


async def update_status(
    conn: AsyncConnection, job_id: UUID, new_status: JobStatus, *, error: str | None = None
) -> Job:
    """Move a job that no worker holds to `new_status` and return it.

    `error` replaces the stored error; leaving it out clears it.

    The move is checked against the transition table inside the UPDATE
    itself, so a job whose status changed in the meantime is not overwritten.
    Raises InvalidTransition when no row was updated.

    A `running` job is never moved here. Only the worker that holds it moves
    it, through `leave_running` or `fail` in `runtime.store.held`, which check
    the lease epoch. A job becomes `running` only through `claim_job`.
    """
    if new_status is JobStatus.RUNNING:
        raise ValueError("a job becomes running through claim_job, not update_status")
    sources = sources_of(new_status) - {JobStatus.RUNNING}
    result = await conn.execute(
        update(jobs)
        .where(jobs.c.id == job_id, jobs.c.status.in_(sources))
        .values(status=new_status, error=error)
        .returning(*job_columns)
    )
    row = result.one_or_none()
    if row is None:
        raise InvalidTransition(job_id, new_status)
    return job_from_row(row)


async def open_question(conn: AsyncConnection, job_id: UUID) -> tuple[int, dict[str, Any]] | None:
    """The `seq` and payload of the job's last `approval_requested` step, or None.

    The `seq` is the question's number: an answer names it (ADR 0012).
    """
    found = (
        await conn.execute(
            select(steps.c.seq, steps.c.payload)
            .where(steps.c.job_id == job_id, steps.c.kind == StepKind.APPROVAL_REQUESTED.value)
            .order_by(steps.c.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    return None if found is None else (found.seq, found.payload)


async def _answer(conn: AsyncConnection, job_id: UUID, kind: StepKind, question: int) -> Job:
    """Record a person's answer to the question a job waits on, and queue the job.

    `question` is the `seq` of the `approval_requested` step the person
    answers (ADR 0012). The job moves from `needs_approval` to `queued` and
    gets a step of `kind` that names the question and its call. Both happen
    in the caller's transaction.

    Raises InvalidTransition when the job does not exist or is not waiting
    for an answer, and OtherQuestion when it waits on another question than
    `question`. Nothing is written then. The job's row is locked before
    anything is checked: of two answers sent at once, one waits for the
    other and is then refused.
    """
    status = (
        await conn.execute(select(jobs.c.status).where(jobs.c.id == job_id).with_for_update())
    ).scalar_one_or_none()
    if status != JobStatus.NEEDS_APPROVAL.value:
        raise InvalidTransition(job_id, JobStatus.QUEUED)
    asked = await open_question(conn, job_id)
    if asked is None or asked[0] != question:
        raise OtherQuestion(job_id, question, None if asked is None else asked[0])
    row = (
        await conn.execute(
            update(jobs)
            .where(jobs.c.id == job_id, jobs.c.status == JobStatus.NEEDS_APPROVAL.value)
            .values(status=JobStatus.QUEUED.value)
            .returning(*job_columns)
        )
    ).one()
    job = job_from_row(row)
    payload = asked[1]
    answer: dict[str, Any] = {
        key: payload[key] for key in ("tool_use_id", "operation_id") if key in payload
    }
    answer["question"] = question
    await _append_next_step(conn, job, kind, answer)
    return job


async def approve_job(conn: AsyncConnection, job_id: UUID, question: int) -> Job:
    """Approve the tool call of question `question`, and return the job, now `queued`.

    Writes an `approval_given` step. The worker that claims the job next
    runs the call: for the first time if the tool needs approval before it
    runs (ADR 0011), again if the call was interrupted (ADR 0005). The
    approval covers that one run. Raises as `_answer` does.
    """
    return await _answer(conn, job_id, StepKind.APPROVAL_GIVEN, question)


async def deny_job(conn: AsyncConnection, job_id: UUID, question: int) -> Job:
    """Refuse the tool call of question `question`, and return the job, now `queued`.

    Writes an `approval_denied` step. The worker that claims the job next
    does not run the call: it stores an error result that says so, and the
    job goes on. Raises as `_answer` does.
    """
    return await _answer(conn, job_id, StepKind.APPROVAL_DENIED, question)


class JobNotFound(Exception):
    """No job has this id."""


def _unheld() -> ColumnElement[bool]:
    """A job that no worker holds: waiting for one, or `running` under a lease that is over.

    A worker whose lease is over can no longer write: every write is fenced
    on `running` and its epoch.
    """
    # Constants in the SQL, so the planner can match the deadline index.
    return or_(
        jobs.c.status.in_([_literal(status.value) for status in _UNHELD]),
        and_(
            jobs.c.status == _literal(JobStatus.RUNNING.value),
            jobs.c.lease_expires_at < func.clock_timestamp(),
        ),
    )


async def cancel_job(conn: AsyncConnection, job_id: UUID) -> tuple[Job, bool]:
    """Cancel a job, or ask the worker that holds it to. Returns the job and whether it is done.

    One statement does it. A job that no worker holds is `cancelled` by it:
    one that waits (`queued`, `waiting`, `needs_approval`), or a `running`
    one whose lease is over. That cancel also writes a `status_changed` step,
    in the caller's transaction. A `running` job under a live lease gets `cancel_requested`:
    its worker sees the flag within a second and stops it. A job that is
    already `cancelled` is returned as it is. Raises JobNotFound, or
    InvalidTransition for a job that ended otherwise.
    """
    unheld = _unheld()
    result = await conn.execute(
        update(jobs)
        .where(
            jobs.c.id == job_id,
            jobs.c.status.not_in([status.value for status in FINAL_STATUSES]),
        )
        .values(
            status=case((unheld, JobStatus.CANCELLED.value), else_=jobs.c.status),
            cancel_requested=True,
            lease_owner=case((unheld, None), else_=jobs.c.lease_owner),
            lease_expires_at=case((unheld, None), else_=jobs.c.lease_expires_at),
        )
        .returning(*job_columns, PREVIOUS_STATUS)
    )
    row = result.one_or_none()
    if row is not None:
        job = job_from_row(row)
        if job.status is JobStatus.CANCELLED:
            # In the caller's transaction, under the row lock of the UPDATE.
            await _append_next_step(
                conn,
                job,
                StepKind.STATUS_CHANGED,
                status_change(row.previous_status, job, StatusCause.CANCEL_REQUESTED),
            )
        return job, job.status is JobStatus.CANCELLED
    found = await get_job(conn, job_id)
    if found is None:
        raise JobNotFound(job_id)
    if found.status is JobStatus.CANCELLED:
        return found, True
    raise InvalidTransition(job_id, JobStatus.CANCELLED)


async def cancel_wanted(conn: AsyncConnection, job_id: UUID) -> bool:
    """True when a cancel of the job was requested."""
    result = await conn.execute(select(jobs.c.cancel_requested).where(jobs.c.id == job_id))
    return bool(result.scalar_one_or_none())


def overdue_statement() -> Select[tuple[UUID]]:
    """The jobs that no worker holds and whose deadline passed. Separate so a test can plan it."""
    return (
        select(jobs.c.id)
        .where(_unheld(), jobs.c.deadline <= func.clock_timestamp())
        # Rows another worker is ending, claiming or cancelling are left to
        # it: no waiting, and no two workers lock the same rows in turn.
        .with_for_update(skip_locked=True)
    )


async def fail_overdue(conn: AsyncConnection) -> list[Job]:
    """Fail every job past its deadline that no worker holds, each with an `error` step.

    No worker holds a job that waits (`queued`, `waiting`, `needs_approval`)
    or a `running` one whose lease is over. A job under a live lease is its
    worker's to fail. The rows stay locked until the caller's transaction
    ends, so the steps cannot race a worker. Returns the jobs.
    """
    result = await conn.execute(
        update(jobs)
        .where(jobs.c.id.in_(overdue_statement()))
        .values(
            status=JobStatus.FAILED,
            error=DEADLINE_ERROR,
            lease_owner=None,
            lease_expires_at=None,
        )
        .returning(*job_columns)
    )
    ended = [job_from_row(row) for row in result]
    for job in ended:
        await _append_next_step(conn, job, StepKind.ERROR, {"message": DEADLINE_ERROR})
        log.warning("job failed at its deadline while waiting", extra={"job_id": job.id})
    return ended
