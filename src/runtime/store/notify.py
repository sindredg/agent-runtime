"""The statements that deliver a parked job's question to the receiver (ADR 0029).

The park writes a row in `notifications`. Any worker with a receiver set
claims one due row at a time, posts it, and records the answer here. No
transaction is open during the POST: the claim moves the row's next try
12 s ahead, and that push is all that keeps a second worker away from it.
A worker that stops during the POST leaves a row that comes due again.

A row is open while its job waits on its question: the job is
`needs_approval` and the row's question is the job's last
`approval_requested` step. Any other open row is closed without a POST.
"""

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, func, not_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncConnection

from runtime.store.held import QuestionReason
from runtime.store.jobs import _append_next_step, job_from_row
from runtime.store.schema import job_columns, jobs, notifications, steps
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind

# How far a claim moves the next try: past the POST's 10 s deadline, so a
# row is not claimed again while its POST may still arrive.
CLAIM_AHEAD = timedelta(seconds=12)


@dataclass(frozen=True, slots=True)
class Due:
    """A claimed row: what to post, and the claim's count of tries."""

    job_id: UUID
    question: int
    reason: QuestionReason
    # Tries so far, this one included.
    attempts: int
    # The name in the question's step: a tool of the worker, or one the
    # job's spec names. Never other text of the model's.
    tool: str


def _waits() -> ColumnElement[bool]:
    """The row's job still waits on the row's question."""
    last_question = (
        select(func.max(steps.c.seq))
        .where(
            steps.c.job_id == notifications.c.job_id,
            steps.c.kind == StepKind.APPROVAL_REQUESTED.value,
        )
        .scalar_subquery()
    )
    parked = exists().where(
        jobs.c.id == notifications.c.job_id,
        jobs.c.status == JobStatus.NEEDS_APPROVAL.value,
    )
    return and_(parked, last_question == notifications.c.question)


async def close_settled(conn: AsyncConnection) -> int:
    """Close, without a POST, every open row whose job no longer waits on it. Returns how many.

    Its question was answered, or its job ended. `delivered_at` stays
    empty, so the rows that were delivered can still be counted.
    """
    result = await conn.execute(
        update(notifications)
        .where(notifications.c.closed_at.is_(None), not_(_waits()))
        .values(closed_at=func.now())
    )
    return result.rowcount


async def claim_due(conn: AsyncConnection) -> Due | None:
    """Claim the open row that has been due longest, if any, and count the try.

    One row: rows are posted one after another, and a later row of a batch
    could pass its 12 s push while those before it were sent. Rows another
    worker is claiming are skipped. Run it in a transaction of its own and
    commit before the POST: `now()` is that transaction's start.
    """
    due = (
        select(notifications.c.job_id, notifications.c.question)
        .where(
            notifications.c.closed_at.is_(None),
            notifications.c.next_try <= func.now(),
            _waits(),
        )
        .order_by(notifications.c.next_try)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    tool = (
        select(steps.c.payload["name"].astext)
        .where(steps.c.job_id == notifications.c.job_id, steps.c.seq == notifications.c.question)
        .scalar_subquery()
    )
    row = (
        await conn.execute(
            update(notifications)
            .where(tuple_(notifications.c.job_id, notifications.c.question).in_(due))
            .values(
                attempts=notifications.c.attempts + 1,
                next_try=func.now() + CLAIM_AHEAD,
            )
            .returning(
                notifications.c.job_id,
                notifications.c.question,
                notifications.c.reason,
                notifications.c.attempts,
                tool.label("tool"),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return Due(row.job_id, row.question, QuestionReason(row.reason), row.attempts, row.tool)


def retry_in(attempts: int) -> timedelta:
    """The wait after the `attempts`-th failed try: 2, 4, 8, 16, then 30 s."""
    return timedelta(seconds=2**attempts if attempts <= 4 else 30)


async def record_failed(conn: AsyncConnection, due: Due) -> bool:
    """Set the next try of a row whose POST failed. False when the row moved on meanwhile.

    It moved on when it was closed, or claimed again after its push ran out.
    """
    result = await conn.execute(
        update(notifications)
        .where(
            notifications.c.job_id == due.job_id,
            notifications.c.question == due.question,
            notifications.c.attempts == due.attempts,
            notifications.c.closed_at.is_(None),
        )
        .values(next_try=func.now() + retry_in(due.attempts))
    )
    return result.rowcount == 1


async def record_delivered(conn: AsyncConnection, due: Due) -> bool:
    """Close a row the receiver took, and log it on the job if it still waits. True if logged.

    No worker holds a parked job, so the step is written under the job row's
    lock, as an answer is: approve, deny, cancel and the deadline all lock
    that row before they move the job, and the step's `seq` cannot be taken
    in between. The notification row is locked after the job row, in the
    order the park takes them. A row that is closed already writes nothing:
    a repeat that arrived late adds no second step.
    """
    job_row = (
        await conn.execute(
            select(*job_columns).where(jobs.c.id == due.job_id).with_for_update(of=jobs)
        )
    ).one_or_none()
    closed = (
        await conn.execute(
            select(notifications.c.closed_at)
            .where(
                notifications.c.job_id == due.job_id,
                notifications.c.question == due.question,
            )
            .with_for_update()
        )
    ).one_or_none()
    if job_row is None or closed is None or closed.closed_at is not None:
        return False
    still_waits = (
        await conn.execute(
            select(_waits()).where(
                notifications.c.job_id == due.job_id,
                notifications.c.question == due.question,
            )
        )
    ).scalar_one()
    await conn.execute(
        update(notifications)
        .where(
            notifications.c.job_id == due.job_id,
            notifications.c.question == due.question,
        )
        .values(delivered_at=func.now(), closed_at=func.now())
    )
    if still_waits:
        await _append_next_step(
            conn,
            job_from_row(job_row),
            StepKind.NOTIFICATION_SENT,
            {"question": due.question},
        )
    return bool(still_waits)
