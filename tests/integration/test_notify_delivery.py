"""Delivering a parked job's question (ADR 0029): claim, POST, record.

The receiver is the fake receiver, in the test's own event loop. The
workers' loop runs here as one round, `deliver_due`, against a database of
the test's own.
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from fake_receiver.app import create_app
from fake_receiver.serve import serving
from runtime.store import held
from runtime.store.held import Question, QuestionReason
from runtime.store.jobs import Job, approve_job, cancel_job, claim_job, deny_job
from runtime.store.notify import (
    CLAIM_AHEAD,
    Due,
    claim_due,
    close_settled,
    record_delivered,
    record_failed,
)
from runtime.store.schema import notifications
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind, read_steps
from runtime.worker.notify import deliver_due

from .conftest import ask, database_now, new_job, with_engine

S = JobStatus
PATH = "/notifications"


async def parked_job(engine: AsyncEngine, questions: int = 1) -> tuple[Job, Question]:
    """A job parked on its last question, with a row in `notifications` for it."""
    async with engine.begin() as conn:
        await new_job(conn)
        job = await claim_job(conn, worker_id="w1", lease=timedelta(minutes=5))
    assert job is not None
    for _ in range(questions):
        question = await ask(engine, job.id, job.lease_epoch, QuestionReason.INTERRUPTED)
    parked = await held.leave_running(
        engine,
        job_id=job.id,
        lease_epoch=job.lease_epoch,
        new_status=S.NEEDS_APPROVAL,
        question=question,
    )
    return parked, question


async def row(engine: AsyncEngine, job_id: UUID) -> dict[str, Any]:
    async with engine.connect() as conn:
        found = await conn.execute(select(notifications).where(notifications.c.job_id == job_id))
        return dict(found.one()._mapping)


async def kinds(engine: AsyncEngine, job_id: UUID) -> list[str]:
    async with engine.connect() as conn:
        return [step.kind.value for step in await read_steps(conn, job_id)]


async def arrivals(url: str) -> list[dict[str, Any]]:
    async with httpx2.AsyncClient() as http:
        found: list[dict[str, Any]] = (await http.get(f"{url}{PATH}")).json()
        return found


async def queue(url: str, *answers: dict[str, Any]) -> None:
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"{url}/_fake/answers", json={"answers": list(answers)})
        response.raise_for_status()


async def make_due(engine: AsyncEngine, job_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(notifications)
            .where(notifications.c.job_id == job_id)
            .values(next_try=text("now() - interval '1 second'"))
        )


async def now(engine: AsyncEngine) -> datetime:
    async with engine.connect() as conn:
        return await database_now(conn)


def test_a_question_is_posted_once_and_logged_on_the_job(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Job, Question, Any, Any, Any, Any, int]:
        job, question = await parked_job(engine)
        async with serving(create_app()) as url:
            first = await deliver_due(engine, f"{url}{PATH}", asyncio.Event())
            # A second round finds nothing: one POST in a run with no failure.
            second = await deliver_due(engine, f"{url}{PATH}", asyncio.Event())
            sent = await arrivals(url)
        assert (first, second) == (1, 0)
        async with engine.connect() as conn:
            steps = await read_steps(conn, job.id)
        return job, question, sent, await row(engine, job.id), steps, steps[-1].payload, first

    job, question, sent, found, steps, payload, _ = with_engine(database_url, body)
    assert [a["body"] for a in sent] == [
        {
            "id": f"{job.id}:{question.seq}",
            "job_id": str(job.id),
            "question": question.seq,
            "tool": "run_shell",
            "reason": "interrupted",
        }
    ]
    assert found["attempts"] == 1
    assert found["delivered_at"] is not None
    assert found["closed_at"] == found["delivered_at"]
    assert steps[-1].kind is StepKind.NOTIFICATION_SENT
    assert payload == {"question": question.seq}
    # The job still waits: the step is a record, not a move.
    assert [s.kind for s in steps].count(StepKind.NOTIFICATION_SENT) == 1


@pytest.mark.parametrize("status", [503, 404, 307])
def test_a_failed_post_is_tried_again_later_and_not_logged(database_url: str, status: int) -> None:
    """Anything but a 2xx is a failure, a redirect too: the address called is the configured one."""

    async def body(engine: AsyncEngine) -> tuple[dict[str, Any], datetime, list[str], Any, Any]:
        job, _ = await parked_job(engine)
        async with serving(create_app()) as url:
            await queue(url, {"status": status})
            before = await now(engine)
            assert await deliver_due(engine, f"{url}{PATH}", asyncio.Event()) == 0
            failed = await row(engine, job.id)
            failed_kinds = await kinds(engine, job.id)
            # Not due yet: nothing is sent.
            assert await deliver_due(engine, f"{url}{PATH}", asyncio.Event()) == 0
            await make_due(engine, job.id)
            assert await deliver_due(engine, f"{url}{PATH}", asyncio.Event()) == 1
            sent = [a["status"] for a in await arrivals(url)]
        return failed, before, failed_kinds, sent, await row(engine, job.id)

    failed, before, failed_kinds, sent, delivered = with_engine(database_url, body)
    assert failed["attempts"] == 1
    assert failed["closed_at"] is None and failed["delivered_at"] is None
    # 2 s after the first failure, by the database clock.
    wait = (failed["next_try"] - before).total_seconds()
    assert 1.5 < wait < 4
    assert StepKind.NOTIFICATION_SENT.value not in failed_kinds
    assert sent == [status, 200]
    assert delivered["attempts"] == 2
    assert delivered["delivered_at"] is not None


@pytest.mark.parametrize("answer", [approve_job, deny_job])
def test_an_answered_question_is_closed_without_a_post(database_url: str, answer: Any) -> None:
    async def body(engine: AsyncEngine) -> tuple[dict[str, Any], list[Any], list[str]]:
        job, question = await parked_job(engine)
        async with engine.begin() as conn:
            await answer(conn, job.id, question.seq)
        async with serving(create_app()) as url:
            assert await deliver_due(engine, f"{url}{PATH}", asyncio.Event()) == 0
            sent = await arrivals(url)
        return await row(engine, job.id), sent, await kinds(engine, job.id)

    found, sent, logged = with_engine(database_url, body)
    assert sent == []
    assert found["closed_at"] is not None
    assert found["delivered_at"] is None
    assert found["attempts"] == 0
    assert StepKind.NOTIFICATION_SENT.value not in logged


def test_a_question_of_a_cancelled_job_is_closed_without_a_post(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[dict[str, Any], list[Any]]:
        job, _ = await parked_job(engine)
        async with engine.begin() as conn:
            await cancel_job(conn, job.id)
        async with serving(create_app()) as url:
            await deliver_due(engine, f"{url}{PATH}", asyncio.Event())
            return await row(engine, job.id), await arrivals(url)

    found, sent = with_engine(database_url, body)
    assert sent == []
    assert found["closed_at"] is not None and found["delivered_at"] is None


def test_the_close_leaves_the_question_a_job_waits_on_open(database_url: str) -> None:
    """A false close is a question nobody is told about: the miss G12 is for."""

    async def body(engine: AsyncEngine) -> tuple[int, dict[str, Any]]:
        job, _ = await parked_job(engine)
        async with engine.begin() as conn:
            closed = await close_settled(conn)
        return closed, await row(engine, job.id)

    closed, found = with_engine(database_url, body)
    assert closed == 0
    assert found["closed_at"] is None


def test_a_row_for_an_earlier_question_is_closed(database_url: str) -> None:
    """The job was asked again, and waits on the later question."""

    async def body(engine: AsyncEngine) -> tuple[int, dict[str, Any], dict[str, Any]]:
        job, question = await parked_job(engine, questions=2)
        # A row for the first question, as a park on it would have left.
        async with engine.begin() as conn:
            await conn.execute(
                notifications.insert().values(
                    job_id=job.id, question=question.seq - 1, reason="interrupted"
                )
            )
        async with engine.begin() as conn:
            closed = await close_settled(conn)
        async with engine.connect() as conn:
            found = {
                r.question: dict(r._mapping)
                for r in await conn.execute(
                    select(notifications).where(notifications.c.job_id == job.id)
                )
            }
        return closed, found[question.seq - 1], found[question.seq]

    closed, earlier, current = with_engine(database_url, body)
    assert closed == 1
    assert earlier["closed_at"] is not None
    assert current["closed_at"] is None


@pytest.mark.parametrize("answer", [approve_job, cancel_job])
def test_a_claim_passes_over_a_row_whose_job_no_longer_waits(
    database_url: str, answer: Any
) -> None:
    """An answer can commit between a round's close and its claim: the claim checks again."""

    async def body(engine: AsyncEngine) -> tuple[Due | None, dict[str, Any]]:
        job, question = await parked_job(engine)
        async with engine.begin() as conn:
            if answer is cancel_job:
                await cancel_job(conn, job.id)
            else:
                await answer(conn, job.id, question.seq)
        async with engine.begin() as conn:
            due = await claim_due(conn)
        return due, await row(engine, job.id)

    due, found = with_engine(database_url, body)
    assert due is None
    assert found["attempts"] == 0


def test_a_claim_takes_one_row(database_url: str) -> None:
    """A batch would leave its later rows waiting while the first were posted, past their push."""

    async def body(engine: AsyncEngine) -> tuple[Due | None, list[dict[str, Any]]]:
        first, _ = await parked_job(engine)
        second, _ = await parked_job(engine)
        async with engine.begin() as conn:
            due = await claim_due(conn)
        return due, [await row(engine, first.id), await row(engine, second.id)]

    due, found = with_engine(database_url, body)
    assert due is not None
    assert sorted(r["attempts"] for r in found) == [0, 1]


def test_a_claim_pushes_the_next_try_12_seconds_and_skips_a_locked_row(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[Due | None, Due | None, Due | None, float]:
        job, _ = await parked_job(engine)
        async with engine.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(
                select(notifications.c.job_id)
                .where(notifications.c.job_id == job.id)
                .with_for_update()
            )
            async with engine.begin() as conn:
                skipped = await claim_due(conn)
            await transaction.rollback()
        before = await now(engine)
        async with engine.begin() as conn:
            claimed = await claim_due(conn)
        async with engine.begin() as conn:
            again = await claim_due(conn)
        ahead = ((await row(engine, job.id))["next_try"] - before).total_seconds()
        return skipped, claimed, again, ahead

    skipped, claimed, again, ahead = with_engine(database_url, body)
    assert skipped is None
    assert claimed is not None and claimed.attempts == 1
    assert claimed.reason is QuestionReason.INTERRUPTED and claimed.tool == "run_shell"
    # Claimed: not due again before its push has run out.
    assert again is None
    assert abs(ahead - CLAIM_AHEAD.total_seconds()) < 1


def test_a_failure_recorded_after_the_row_moved_on_changes_nothing(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[bool, dict[str, Any], dict[str, Any]]:
        job, _ = await parked_job(engine)
        async with engine.begin() as conn:
            first = await claim_due(conn)
        assert first is not None
        await make_due(engine, job.id)
        async with engine.begin() as conn:
            assert await claim_due(conn) is not None
        before = await row(engine, job.id)
        async with engine.begin() as conn:
            recorded = await record_failed(conn, first)
        return recorded, before, await row(engine, job.id)

    recorded, before, after = with_engine(database_url, body)
    assert recorded is False
    assert after == before


def test_a_late_repeat_of_a_delivery_writes_no_second_step(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[bool, bool, list[str]]:
        job, _ = await parked_job(engine)
        async with engine.begin() as conn:
            due = await claim_due(conn)
        assert due is not None
        async with engine.begin() as conn:
            first = await record_delivered(conn, due)
        async with engine.begin() as conn:
            second = await record_delivered(conn, due)
        return first, second, await kinds(engine, job.id)

    first, second, logged = with_engine(database_url, body)
    assert (first, second) == (True, False)
    assert logged.count(StepKind.NOTIFICATION_SENT.value) == 1


def test_a_delivery_waits_for_an_answer_in_flight_and_then_writes_no_step(
    database_url: str,
) -> None:
    """The delivery takes the job row's lock, as an answer does.

    Without it, the delivery would read the job as still waiting and take
    the `seq` the uncommitted answer holds: its step would collide with the
    answer's, or come after an answer that already moved the job.
    """

    async def body(engine: AsyncEngine) -> tuple[bool, bool, list[str]]:
        job, question = await parked_job(engine)
        async with engine.begin() as conn:
            due = await claim_due(conn)
        assert due is not None
        async with engine.connect() as answering:
            transaction = await answering.begin()
            await approve_job(answering, job.id, question.seq)

            async def deliver() -> bool:
                async with engine.begin() as conn:
                    return await record_delivered(conn, due)

            delivery = asyncio.create_task(deliver())
            await asyncio.sleep(0.5)
            waited = not delivery.done()
            await transaction.commit()
        logged = await delivery
        return waited, logged, await kinds(engine, job.id)

    waited, logged, found = with_engine(database_url, body)
    assert waited
    assert logged is False
    assert found[-1] == StepKind.APPROVAL_GIVEN.value
    assert StepKind.NOTIFICATION_SENT.value not in found


def test_an_unreachable_receiver_is_a_failure_and_its_address_is_not_logged(
    database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A webhook's address can hold a token. No log record carries it."""

    async def body(engine: AsyncEngine) -> tuple[int, dict[str, Any]]:
        job, _ = await parked_job(engine)
        # Port 9 of 127.0.0.1 answers nothing.
        url = "http://127.0.0.1:9/hooks/tokenInPath?key=tokenInQuery"
        delivered = await deliver_due(engine, url, asyncio.Event())
        return delivered, await row(engine, job.id)

    with caplog.at_level("DEBUG"):
        delivered, found = with_engine(database_url, body)
    assert delivered == 0
    assert found["attempts"] == 1 and found["closed_at"] is None
    failures = [r for r in caplog.records if r.getMessage() == "notification not delivered"]
    assert len(failures) == 1
    for record in caplog.records:
        assert "tokenIn" not in str(record.__dict__), record.getMessage()


def test_a_stop_during_a_post_leaves_the_row_to_come_due_again(database_url: str) -> None:
    async def body(engine: AsyncEngine) -> tuple[int, dict[str, Any], datetime, float]:
        job, _ = await parked_job(engine)
        stop = asyncio.Event()
        async with serving(create_app()) as url:
            await queue(url, {"hang": 30})
            asyncio.get_running_loop().call_later(0.5, stop.set)
            before = await now(engine)
            started = time.monotonic()
            delivered = await deliver_due(engine, f"{url}{PATH}", stop)
            took = time.monotonic() - started
        return delivered, await row(engine, job.id), before, took

    delivered, found, before, took = with_engine(database_url, body)
    # Given up at the stop, not at the POST's 10 s deadline.
    assert took < 5
    assert delivered == 0
    assert found["attempts"] == 1 and found["closed_at"] is None
    # Due again once the claim's push has run out.
    assert (found["next_try"] - before).total_seconds() > 10
