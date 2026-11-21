"""The job list: many jobs in one read, newest first.

The order is by id. Ids are made by the database with `uuidv7()`, so they
sort by the time they were made, and one id is enough to say where the next
page starts. `created_at` is the start of the inserting transaction and can
differ from that order by the length of a transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select, true
from sqlalchemy.ext.asyncio import AsyncConnection

from runtime.store.schema import jobs, steps
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind


@dataclass(frozen=True, slots=True)
class Question:
    """What a job in `needs_approval` waits on, without the call's input.

    The input is written by the model and can be large. It is read with the
    job's steps.
    """

    # The `seq` of the `approval_requested` step: an answer names it (ADR 0012).
    seq: int
    tool: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class JobSummary:
    """A job as the list shows it: no spec, no lease."""

    id: UUID
    status: JobStatus
    token_budget: int
    tokens_used: int
    deadline: datetime
    error: str | None
    created_at: datetime
    question: Question | None


def list_statement(*, status: JobStatus | None, before: UUID | None, limit: int) -> Select[tuple]:  # type: ignore[type-arg]
    # The last question of a job that waits, as `open_question` reads it. The
    # status test is inside the subquery, so the steps of a job that does
    # not wait are never read.
    asked = (
        select(
            steps.c.seq,
            steps.c.payload["name"].astext.label("tool"),
            steps.c.payload["reason"].astext.label("reason"),
        )
        .where(
            jobs.c.status == JobStatus.NEEDS_APPROVAL.value,
            steps.c.job_id == jobs.c.id,
            steps.c.kind == StepKind.APPROVAL_REQUESTED.value,
        )
        .order_by(steps.c.seq.desc())
        .limit(1)
        .lateral("asked")
    )
    statement = (
        select(
            jobs.c.id,
            jobs.c.status,
            jobs.c.token_budget,
            jobs.c.tokens_used,
            jobs.c.deadline,
            jobs.c.error,
            jobs.c.created_at,
            asked.c.seq.label("question_seq"),
            asked.c.tool.label("question_tool"),
            asked.c.reason.label("question_reason"),
        )
        .select_from(jobs.outerjoin(asked, true()))
        .order_by(jobs.c.id.desc())
        .limit(limit)
    )
    if status is not None:
        statement = statement.where(jobs.c.status == status.value)
    if before is not None:
        statement = statement.where(jobs.c.id < before)
    return statement


async def list_jobs(
    conn: AsyncConnection,
    *,
    status: JobStatus | None = None,
    before: UUID | None = None,
    limit: int,
) -> list[JobSummary]:
    """At most `limit` jobs, newest first.

    `status` keeps the jobs in that status. `before` keeps the jobs older
    than the job with that id: the last id of one page starts the next. The
    id does not have to exist.
    """
    rows = await conn.execute(list_statement(status=status, before=before, limit=limit))
    return [
        JobSummary(
            id=row.id,
            status=JobStatus(row.status),
            token_budget=row.token_budget,
            tokens_used=row.tokens_used,
            deadline=row.deadline,
            error=row.error,
            created_at=row.created_at,
            question=None
            if row.question_seq is None
            else Question(row.question_seq, row.question_tool, row.question_reason),
        )
        for row in rows
    ]
