"""The job list in the store, on real Postgres: order, filter, paging, and
the question of a job that waits."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.store import held
from runtime.store.jobs import Job, approve_job, cancel_job, claim_job, create_job
from runtime.store.listing import JobSummary, Question, list_jobs, list_statement
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind, read_steps

from .conftest import with_engine

S = JobStatus
REASON = "the tool needs approval before it runs"


async def ask(engine: AsyncEngine, job: Job, tool: str = "run_shell") -> int:
    """Write the job's next step as a question and park the job. Returns the question's seq."""
    async with engine.connect() as conn:
        seq = len(await read_steps(conn, job.id)) + 1
    asked = {
        "tool_use_id": f"toolu_{seq}",
        "name": tool,
        "input": {"command": "id"},
        "operation_id": f"{job.id}:toolu_{seq}",
        "reason": REASON,
    }
    await held.append_step(
        engine,
        job_id=job.id,
        lease_epoch=job.lease_epoch,
        seq=seq,
        kind=StepKind.APPROVAL_REQUESTED,
        payload=asked,
    )
    await held.leave_running(
        engine, job_id=job.id, lease_epoch=job.lease_epoch, new_status=S.NEEDS_APPROVAL
    )
    return seq


async def job_in(engine: AsyncEngine, status: JobStatus) -> Job:
    """A new job, moved to `status` the way the API and a worker move it.

    A queued job is claimed by the next call, so queued jobs are made last.
    """
    async with engine.begin() as conn:
        job = await create_job(
            conn,
            spec={"prompt": "go"},
            token_budget=1000,
            deadline=datetime.now(UTC) + timedelta(hours=1),
        )
        if status is S.QUEUED:
            return job
        claimed = await claim_job(conn, worker_id="test")
    assert claimed is not None and claimed.id == job.id
    if status is S.RUNNING:
        return claimed
    if status is S.NEEDS_APPROVAL:
        await ask(engine, claimed)
    elif status is S.FAILED:
        await held.fail(engine, job_id=job.id, lease_epoch=claimed.lease_epoch, error="boom")
    elif status is S.CANCELLED:
        await held.leave_running(
            engine, job_id=job.id, lease_epoch=claimed.lease_epoch, new_status=S.WAITING, wait=WAIT
        )
        async with engine.begin() as conn:
            await cancel_job(conn, job.id)
    else:
        await held.leave_running(
            engine,
            job_id=job.id,
            lease_epoch=claimed.lease_epoch,
            new_status=status,
            wait=WAIT if status is S.WAITING else None,
        )
    return claimed


WAIT = held.Wait(seconds=60, attempt=1, retry_after=None, error="busy")
# Interleaved, so a filter has to pass over other statuses. Queued last.
MIX = [
    S.SUCCEEDED,
    S.NEEDS_APPROVAL,
    S.RUNNING,
    S.FAILED,
    S.NEEDS_APPROVAL,
    S.WAITING,
    S.CANCELLED,
    S.SUCCEEDED,
    S.QUEUED,
    S.QUEUED,
]


async def listed(engine: AsyncEngine, **query: Any) -> list[JobSummary]:
    async with engine.connect() as conn:
        return await list_jobs(conn, **{"limit": 100, **query})


def test_jobs_are_listed_newest_first(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[UUID], list[JobSummary]]:
        made = [(await job_in(engine, status)).id for status in MIX]
        return made, await listed(engine)

    made, found = with_engine(database_url, body)

    # Against the order the jobs were made in, not against a column the
    # list could have sorted by.
    assert [job.id for job in found] == made[::-1]
    assert [job.status for job in found] == MIX[::-1]


@pytest.mark.parametrize("status", list(JobStatus))
def test_a_status_keeps_the_jobs_in_that_status_newest_first(
    database_url: str, status: JobStatus
) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[UUID], list[JobSummary]]:
        made = [(await job_in(engine, each)).id for each in MIX]
        return made, await listed(engine, status=status)

    made, found = with_engine(database_url, body)

    expected = [job_id for job_id, each in zip(made, MIX, strict=True) if each is status]
    assert expected, "the mix has every status"
    assert [job.id for job in found] == expected[::-1]
    assert {job.status for job in found} == {status}


def test_pages_follow_each_other_without_a_gap_or_a_repeat(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[UUID], list[list[UUID]]]:
        made = [(await job_in(engine, S.SUCCEEDED)).id for _ in range(7)]
        pages: list[list[UUID]] = []
        before = None
        while page := await listed(engine, limit=3, before=before):
            pages.append([job.id for job in page])
            before = page[-1].id
        return made, pages

    made, pages = with_engine(database_url, body)

    assert [len(page) for page in pages] == [3, 3, 1]
    assert [job_id for page in pages for job_id in page] == made[::-1]


def test_before_and_a_status_page_through_the_jobs_in_that_status(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[list[UUID], list[JobSummary]]:
        made = [(await job_in(engine, each)).id for each in MIX]
        newest_waiting = made[4]
        return made, await listed(engine, status=S.NEEDS_APPROVAL, before=newest_waiting)

    made, found = with_engine(database_url, body)

    assert [job.id for job in found] == [made[1]]


def test_before_an_id_no_job_has_lists_the_jobs_older_than_it(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, list[JobSummary], list[JobSummary]]:
        job = await job_in(engine, S.QUEUED)
        newer = await listed(engine, before=uuid.UUID(int=2**128 - 1))
        older = await listed(engine, before=uuid.UUID(int=0))
        return job, newer, older

    job, newer, older = with_engine(database_url, body)

    assert [found.id for found in newer] == [job.id]
    assert older == []


def test_a_listed_job_has_its_fields_and_no_question_unless_it_waits(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, list[JobSummary]]:
        job = await job_in(engine, S.FAILED)
        return job, await listed(engine)

    job, found = with_engine(database_url, body)

    assert found == [
        JobSummary(
            id=job.id,
            status=S.FAILED,
            token_budget=1000,
            tokens_used=0,
            deadline=job.deadline,
            error="boom",
            created_at=job.created_at,
            question=None,
        )
    ]


def test_a_waiting_job_is_listed_with_its_question(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> list[JobSummary]:
        await job_in(engine, S.NEEDS_APPROVAL)
        return await listed(engine)

    (found,) = with_engine(database_url, body)

    assert found.question == Question(seq=1, tool="run_shell", reason=REASON)


def test_a_job_on_its_second_question_is_listed_with_that_one(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[int, int, list[JobSummary]]:
        job = await job_in(engine, S.NEEDS_APPROVAL)
        async with engine.begin() as conn:
            await approve_job(conn, job.id, 1)
            again = await claim_job(conn, worker_id="test")
        assert again is not None and again.id == job.id
        between = await listed(engine)
        second = await ask(engine, again, tool="create_issue")
        return between[0].question is None, second, await listed(engine)

    no_question_while_running, second, (found,) = with_engine(database_url, body)

    # Answered and running again: the old question is not shown as open.
    assert no_question_while_running
    assert second > 1
    assert found.question == Question(seq=second, tool="create_issue", reason=REASON)


def test_a_waiting_job_whose_question_cannot_be_read_is_listed_without_one(
    database_url: str,
) -> None:
    """A question's payload without a name or a reason, and a park with no question step."""

    async def body(engine: AsyncEngine) -> list[JobSummary]:
        bare = await job_in(engine, S.RUNNING)
        await held.leave_running(
            engine, job_id=bare.id, lease_epoch=bare.lease_epoch, new_status=S.NEEDS_APPROVAL
        )
        odd = await job_in(engine, S.RUNNING)
        await held.append_step(
            engine,
            job_id=odd.id,
            lease_epoch=odd.lease_epoch,
            seq=1,
            kind=StepKind.APPROVAL_REQUESTED,
            payload={"name": {"not": "text"}},
        )
        await held.leave_running(
            engine, job_id=odd.id, lease_epoch=odd.lease_epoch, new_status=S.NEEDS_APPROVAL
        )
        return await listed(engine)

    odd, bare = with_engine(database_url, body)

    assert bare.question is None
    # Postgres gives the text of a value that is not a string, and NULL for a missing key.
    assert odd.question == Question(seq=1, tool='{"not": "text"}', reason=None)


def plan_of(database_url: str, **query: Any) -> str:
    sql = str(
        list_statement(**{"status": None, "before": None, "limit": 50, **query}).compile(
            dialect=postgresql.asyncpg.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )

    async def body() -> str:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            # The planner prefers a scan and a sort on a table this small.
            await conn.execute("SET enable_seqscan = off; SET enable_sort = off")
            plan = await conn.fetch(f"EXPLAIN {sql}")
        finally:
            await conn.close()
        return "\n".join(row[0] for row in plan)

    return asyncio.run(body())


def test_the_list_with_a_status_reads_the_status_index_in_order(database_url: str) -> None:
    """Finished jobs must not cost the list of waiting jobs anything."""
    plan = plan_of(database_url, status=S.NEEDS_APPROVAL, before=uuid.UUID(int=5))

    assert "Index Scan Backward using jobs_status_id on jobs" in plan
    assert "Seq Scan" not in plan
    assert "Sort" not in plan


def test_the_list_without_a_status_reads_the_primary_key_in_order(database_url: str) -> None:
    plan = plan_of(database_url)

    assert "Index Scan Backward using jobs_pkey on jobs" in plan
    assert "Seq Scan" not in plan
    assert "Sort" not in plan


def test_the_steps_of_a_job_that_does_not_wait_are_not_read(database_url: str) -> None:
    """The status test gates the read of the steps: Postgres plans it as a one-time filter."""
    plan = plan_of(database_url)

    assert "One-Time Filter: (jobs.status = 'needs_approval'::text)" in plan
