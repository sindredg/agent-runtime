"""The step log: an append-only list of events per job.

Functions take a connection and leave the transaction to the caller, as in
`runtime.store.jobs`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from runtime.store.payload import check_json
from runtime.store.schema import steps


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


def _step(row: Row[Any]) -> Step:
    fields = dict(row._mapping)
    fields["kind"] = StepKind(fields["kind"])
    return Step(**fields)


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
    return _step(row)


async def read_steps(conn: AsyncConnection, job_id: UUID, *, after_seq: int = 0) -> list[Step]:
    """The job's steps with `seq` greater than `after_seq`, in order."""
    result = await conn.execute(
        select(steps).where(steps.c.job_id == job_id, steps.c.seq > after_seq).order_by(steps.c.seq)
    )
    return [_step(row) for row in result]
