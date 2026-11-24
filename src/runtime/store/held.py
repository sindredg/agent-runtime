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

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Executable,
    Integer,
    Interval,
    Row,
    Text,
    case,
    func,
    insert,
    literal,
    null,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from runtime.store.jobs import Job, job_from_row
from runtime.store.payload import check_json
from runtime.store.schema import job_columns, jobs, notifications, step_columns, steps
from runtime.store.status import JobStatus, can_move
from runtime.store.steps import (
    StatusCause,
    Step,
    StepConflict,
    StepKind,
    next_seq,
    step_from_row,
)

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
        .returning(*step_columns)
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


# Leaving `running` for these writes a `status_changed` step with the move.
# A release to `queued` is not a decision about the job, and an end with
# `succeeded` follows the model's last answer: both stay log lines.
_RECORDED = frozenset({JobStatus.NEEDS_APPROVAL, JobStatus.CANCELLED, JobStatus.WAITING})
# Moves a requested cancel turns into `cancelled`: nobody else would stop
# such a job before its deadline or its `not_before`.
_CANCELLED_INSTEAD = frozenset({JobStatus.NEEDS_APPROVAL, JobStatus.WAITING})
# Why a job can wait.
_WAIT_CAUSES = frozenset({StatusCause.PROVIDER_BUSY, StatusCause.PROVIDER_ERROR})


@dataclass(frozen=True, slots=True)
class Wait:
    """Why and how long a job waits for the provider (ADR 0018, ADR 0030)."""

    seconds: float
    # The waits since the last response, of either cause, this one included.
    attempt: int
    # The provider's `retry-after`, None without one.
    retry_after: float | None
    error: str
    cause: StatusCause = StatusCause.PROVIDER_BUSY


class QuestionReason(StrEnum):
    """Why a person is asked, as the notification says it: a fixed word.

    The question's own reason text is not sent. For an interrupted call it
    can hold a tool check's error, which the system behind the tool writes.
    """

    # The tool needs approval before each run. Nothing of the call has run.
    BEFORE_RUN = "before_run"
    # A worker went away during the call. It may have run.
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class Question:
    """The question a job is parked on: the `seq` of its `approval_requested` step."""

    seq: int
    reason: QuestionReason


async def leave_running(
    engine: AsyncEngine,
    *,
    job_id: UUID,
    lease_epoch: int,
    new_status: JobStatus,
    wait: Wait | None = None,
    question: Question | None = None,
) -> Job:
    """Move the held job from `running` to `new_status` and give up the lease.

    This is how a worker finishes a job, parks it for an approval, makes it
    wait for the provider, and releases it back to `queued`. A stored
    error is cleared. A job that would be parked for an approval or made to
    wait while its cancel is requested is `cancelled` instead: nobody else
    would stop it before its deadline or its `not_before`. A released job
    keeps the request; the next claim cancels it.

    A move to `waiting` takes `wait`. It sets `not_before` to the database
    clock plus `wait.seconds`, never from the worker's clock.

    A park takes `question`, the question the job waits on, and writes its
    row in `notifications` in the same statement (ADR 0029). A park that
    turns into a cancel writes none. A row for that question that is
    already there stays as it is.

    A park, a wait and a cancel also write a `status_changed` step, in the
    same statement: from `running`, to the status the job got, and the
    cause. A wait's step adds `not_before`, the attempt, the delay, the
    provider's `retry-after` and its error.

    Not to `failed`: that is `fail`, which writes the `error` step with it.
    """
    if new_status is JobStatus.FAILED:
        raise ValueError("a job is failed with held.fail, which also writes its error step")
    if not isinstance(new_status, JobStatus) or not can_move(JobStatus.RUNNING, new_status):
        raise ValueError(f"a running job cannot move to {new_status!r}")
    if (wait is not None) != (new_status is JobStatus.WAITING):
        raise ValueError("a move to waiting takes a wait, and only that move does")
    if (question is not None) != (new_status is JobStatus.NEEDS_APPROVAL):
        raise ValueError("a park takes the question, and only a park does")
    if question is not None and (
        question.seq < 1 or not isinstance(question.reason, QuestionReason)
    ):
        raise ValueError(f"not a question: {question!r}")
    waiting: dict[str, Any] = {}
    if wait is not None:
        if (
            not 0 <= wait.seconds < float("inf")
            or wait.attempt < 1
            or wait.cause not in _WAIT_CAUSES
        ):
            raise ValueError(f"not a wait: {wait!r}")
        waiting = {
            "attempt": wait.attempt,
            "delay_seconds": round(wait.seconds, 3),
            "retry_after": wait.retry_after,
            "error": wait.error,
        }
        check_json(waiting)
    moved = (
        update(jobs)
        .where(*_held(job_id, lease_epoch))
        .values(
            status=(
                case((jobs.c.cancel_requested, JobStatus.CANCELLED.value), else_=new_status.value)
                if new_status in _CANCELLED_INSTEAD
                else new_status.value
            ),
            error=None,
            lease_owner=None,
            lease_expires_at=None,
            not_before=(
                None
                if wait is None
                else case(
                    (jobs.c.cancel_requested, None),
                    else_=func.clock_timestamp()
                    + literal(timedelta(seconds=wait.seconds), Interval),
                )
            ),
        )
        .returning(*job_columns)
    )
    if new_status not in _RECORDED:
        return job_from_row(await _send(engine, moved, job_id, lease_epoch))
    held = moved.cte("held")
    cause = case(
        (held.c.status == JobStatus.CANCELLED.value, StatusCause.CANCEL_REQUESTED.value),
        (
            held.c.status == JobStatus.WAITING.value,
            (wait.cause if wait is not None else StatusCause.PROVIDER_BUSY).value,
        ),
        else_=StatusCause.APPROVAL_REQUESTED.value,
    )
    change: ColumnElement[Any] = func.jsonb_build_object(
        "from", JobStatus.RUNNING.value, "to", held.c.status, "cause", cause
    )
    if waiting:
        # Only a job that really waits gets the details. One whose cancel
        # was requested is cancelled, and its step says only that.
        change = change.op("||")(
            case(
                (
                    held.c.status == JobStatus.WAITING.value,
                    literal(waiting, JSONB).op("||")(
                        func.jsonb_build_object("not_before", held.c.not_before)
                    ),
                ),
                else_=literal({}, JSONB),
            )
        )
    step = (
        insert(steps)
        .from_select(
            ["job_id", "seq", "kind", "payload", "lease_epoch"],
            select(
                held.c.id,
                next_seq(job_id),
                literal(StepKind.STATUS_CHANGED.value, Text),
                change,
                literal(lease_epoch, Integer),
            ),
        )
        .returning(steps.c.seq)
        .cte("step")
    )
    # The step is in a CTE that the main query does not read. Postgres runs
    # it all the same, and in the same statement as the move.
    statement = select(*held.c).add_cte(step)
    if question is not None:
        # Only for a job that was parked: one whose cancel was requested is
        # cancelled by this statement, and nobody needs to be asked.
        # Nothing on a conflict: the row there already tells the person,
        # and an update would reset its tries.
        #
        # The key to `steps` alone would take any step with that seq, the
        # `status_changed` step of this very statement too: a key is checked
        # at its end. So the seq must also be the job's last question, as
        # the log was before this statement. If it is not, the reason is
        # NULL, the column refuses it, and the whole park is undone.
        asked = (
            select(func.max(steps.c.seq))
            .where(
                steps.c.job_id == job_id,
                steps.c.kind == StepKind.APPROVAL_REQUESTED.value,
            )
            .scalar_subquery()
        )
        notice = (
            upsert(notifications)
            .from_select(
                ["job_id", "question", "reason"],
                select(
                    held.c.id,
                    literal(question.seq, Integer),
                    case(
                        (asked == question.seq, literal(question.reason.value, Text)),
                        else_=null(),
                    ),
                ).where(held.c.status == JobStatus.NEEDS_APPROVAL.value),
            )
            .on_conflict_do_nothing(
                index_elements=[notifications.c.job_id, notifications.c.question]
            )
            .returning(notifications.c.question)
            .cte("notice")
        )
        statement = statement.add_cte(notice)
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
        .returning(*step_columns)
    )
    return step_from_row(await _send(engine, statement, job_id, lease_epoch))


__all__ = [
    "LeaseLost",
    "Question",
    "QuestionReason",
    "append_step",
    "fail",
    "leave_running",
    "renew_lease",
]
