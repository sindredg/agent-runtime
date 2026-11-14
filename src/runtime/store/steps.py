"""The step log: an append-only list of events per job.

Functions take a connection and leave the transaction to the caller, as in
`runtime.store.jobs`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Row, ScalarSelect, and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from runtime.store.payload import check_json
from runtime.store.schema import jobs, steps
from runtime.store.status import JobStatus


class StepKind(StrEnum):
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_INTENT = "tool_intent"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GIVEN = "approval_given"


@dataclass(frozen=True, slots=True)
class Step:
    job_id: UUID
    seq: int
    kind: StepKind
    payload: dict[str, Any]
    lease_epoch: int
    created_at: datetime


class StepConflict(Exception):
    """A step with this `seq` already exists for the job."""

    def __init__(self, job_id: UUID, seq: int) -> None:
        super().__init__(f"job {job_id} already has a step with seq {seq}")
        self.job_id = job_id
        self.seq = seq


def step_from_row(row: Row[Any]) -> Step:
    fields = dict(row._mapping)
    fields["kind"] = StepKind(fields["kind"])
    return Step(**fields)


def next_seq(job_id: UUID) -> ScalarSelect[int]:
    """A subquery for the job's next free `seq`: its last one plus one, or 1.

    Only for a statement that holds the job row's lock, or runs in a
    transaction that does. Without the lock two writers can pick the same
    `seq`, and the second is refused by the unique constraint.
    """
    return (
        select(func.coalesce(func.max(steps.c.seq), 0) + 1)
        .where(steps.c.job_id == job_id)
        .scalar_subquery()
    )


async def append_step(
    conn: AsyncConnection,
    *,
    job_id: UUID,
    seq: int,
    kind: StepKind,
    payload: dict[str, Any],
    lease_epoch: int,
) -> Step:
    """Append a step. The caller picks `seq`: the last seq of the job plus one.

    Raises StepConflict if the job already has that `seq`, and leaves the
    existing step as it is. ON CONFLICT DO NOTHING reports the duplicate as
    zero inserted rows, so the caller's transaction stays usable.

    Raises InvalidPayload or ValueError, before anything is sent, for a
    payload jsonb rejects or a `seq` below 1. A `job_id` that does not exist
    fails in the database with IntegrityError, which aborts the transaction.
    """
    if seq < 1:
        raise ValueError("seq starts at 1")
    check_json(payload)
    result = await conn.execute(
        insert(steps)
        .values(job_id=job_id, seq=seq, kind=kind, payload=payload, lease_epoch=lease_epoch)
        .on_conflict_do_nothing(index_elements=[steps.c.job_id, steps.c.seq])
        .returning(*steps.c)
    )
    row = result.one_or_none()
    if row is None:
        raise StepConflict(job_id, seq)
    return step_from_row(row)


async def read_steps(conn: AsyncConnection, job_id: UUID, *, after_seq: int = 0) -> list[Step]:
    """The job's steps with `seq` greater than `after_seq`, in order."""
    result = await conn.execute(
        select(steps).where(steps.c.job_id == job_id, steps.c.seq > after_seq).order_by(steps.c.seq)
    )
    return [step_from_row(row) for row in result]


async def read_progress(
    conn: AsyncConnection, job_id: UUID, *, after_seq: int = 0, limit: int = 1000
) -> tuple[JobStatus, list[Step]] | None:
    """The job's status and up to `limit` steps after `after_seq`, or None if there is no job.

    One statement, so both come from the same snapshot of the database. A
    reader that follows a job relies on this: when the status it gets is
    final and fewer than `limit` steps came with it, it has seen every step.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    result = await conn.execute(
        select(jobs.c.status, *steps.c)
        .select_from(
            jobs.outerjoin(steps, and_(steps.c.job_id == jobs.c.id, steps.c.seq > after_seq))
        )
        .where(jobs.c.id == job_id)
        .order_by(steps.c.seq)
        .limit(limit)
    )
    rows = result.all()
    if not rows:
        return None
    status = JobStatus(rows[0].status)
    found = [
        Step(
            job_id=row.job_id,
            seq=row.seq,
            kind=StepKind(row.kind),
            payload=row.payload,
            lease_epoch=row.lease_epoch,
            created_at=row.created_at,
        )
        # A job without matching steps is one row whose step columns are NULL.
        for row in rows
        if row.seq is not None
    ]
    return status, found
