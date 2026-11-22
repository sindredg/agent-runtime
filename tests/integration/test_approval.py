"""G10: a tool call that needs approval does not run before a person approved it.

Real Postgres. The provider and the tool are the stand-ins of the resume
tests, which keep their own records. Whether a call ran is read from the
tool's record, never from the step log alone. The test is the person: it
answers through the store, as the API does.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import asyncpg
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.jobs import (
    DEADLINE_ERROR,
    InvalidTransition,
    Job,
    OtherQuestion,
    approve_job,
    claim_job,
    deny_job,
    fail_overdue,
    get_job,
    open_question,
)
from runtime.store.schema import notifications
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import CheckOutcome
from runtime.worker.agent import run_job
from runtime.worker.approval import DENIED, DENIED_RERUN

from .conftest import Stack, as_owner, expire_lease
from .test_resume import PARKED, SPEC, TURNS, Acts, Script, op

K = StepKind
S = JobStatus

BY_TOOL = "the tool's calls need a person's approval before they run"
BY_SPEC = "the job's spec asks for a person's approval before this tool runs"


class Person:
    """One job, the workers that run it one after another, and the person who answers."""

    def __init__(self, stack: Stack, engine: AsyncEngine, acts: Acts) -> None:
        self.stack, self.engine, self.acts = stack, engine, acts
        self.script = Script()
        self.submitted: UUID | None = None
        self.workers = 0

    @property
    def job_id(self) -> UUID:
        assert self.submitted is not None
        return self.submitted

    async def submit(self, spec: dict[str, Any] = SPEC) -> None:
        async with self.engine.begin() as conn:
            self.submitted = (await self.stack.create(conn, spec)).id

    async def work(self, *, takeover: bool = False) -> JobStatus | None:
        """One worker claims the job and runs it until it is done with it."""
        self.workers += 1
        async with self.engine.begin() as conn:
            if takeover:
                await expire_lease(conn, self.job_id)
            claim = await claim_job(conn, worker_id=f"w{self.workers}")
        assert claim is not None
        assert claim.id == self.job_id
        return await run_job(
            self.engine, self.script, self.acts.registry(), claim, self.stack.settings()
        )

    async def abandon_in_the_tool(self, *, takeover: bool = False) -> None:
        """A worker claims the job, starts the next tool run, and is gone in the middle of it."""
        self.acts.hang_at = len(self.acts.runs) + 1
        self.acts.hanging.clear()
        self.acts.release.clear()
        worker = asyncio.create_task(self.work(takeover=takeover))
        assert await asyncio.to_thread(self.acts.hanging.wait, 30)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.acts.hang_at = None
        self.acts.release.set()

    async def asked(self) -> int:
        """The number of the question the job waits on: its last `approval_requested` step."""
        async with self.engine.begin() as conn:
            found = await open_question(conn, self.job_id)
        assert found is not None
        return found[0]

    async def approve(self, question: int | None = None) -> None:
        """Approve `question`, or the question the job waits on now."""
        question = question if question is not None else await self.asked()
        async with self.engine.begin() as conn:
            await approve_job(conn, self.job_id, question)

    async def deny(self, question: int | None = None) -> None:
        question = question if question is not None else await self.asked()
        async with self.engine.begin() as conn:
            await deny_job(conn, self.job_id, question)

    async def notices(self) -> list[tuple[int, str]]:
        """The job's rows in `notifications`: the question and the reason word."""
        async with self.engine.connect() as conn:
            found = await conn.execute(
                select(notifications.c.question, notifications.c.reason)
                .where(notifications.c.job_id == self.job_id)
                .order_by(notifications.c.question)
            )
            return [(row.question, row.reason) for row in found]

    async def read(self) -> tuple[Job, list[Step]]:
        async with self.engine.begin() as conn:
            job = await get_job(conn, self.job_id)
            assert job is not None
            return job, await read_steps(conn, job.id)


def with_person[T](stack: Stack, acts: Acts, body: Callable[[Person], Awaitable[T]]) -> T:
    async def run(engine: AsyncEngine, _: AnthropicProvider) -> T:
        return await body(Person(stack, engine, acts))

    return stack.run(run)


def kinds(steps: list[Step]) -> list[StepKind]:
    return [step.kind for step in steps]


def question(job: Job, tool_use_id: str, path: str, reason: str) -> dict[str, Any]:
    return {
        "tool_use_id": tool_use_id,
        "name": "act",
        "input": {"path": path},
        "operation_id": op(job, tool_use_id),
        "reason": reason,
    }


# The gate


def test_a_call_that_needs_approval_does_not_run_before_a_person_approved_it(
    stack: Stack,
) -> None:
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step], Job, list[Step]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        parked = await person.read()
        # The park wrote the row its notification is sent from (ADR 0029).
        assert await person.notices() == [(3, "before_run")]
        # Asked, and nothing of the call has happened: no run, and no intent.
        assert acts.runs == []
        # Nobody may claim a job that waits for a person.
        async with person.engine.begin() as conn:
            assert await claim_job(conn, worker_id="other") is None

        await person.approve()
        # The first call runs. The second is a call of the same tool: asked again.
        assert await person.work() is S.NEEDS_APPROVAL
        assert acts.runs == [(op(parked[0], "toolu_a"), "a")]
        await person.approve()
        assert await person.work() is S.SUCCEEDED
        # One row per question, each written by its park.
        assert await person.notices() == [(3, "before_run"), (8, "before_run")]
        return *parked, *(await person.read())

    parked_job, parked_steps, job, steps = with_person(stack, acts, body)
    assert (parked_job.status, parked_job.lease_owner, parked_job.error) == (
        S.NEEDS_APPROVAL,
        None,
        None,
    )
    assert kinds(parked_steps) == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
    ]
    assert parked_steps[2].payload == question(job, "toolu_a", "a", BY_TOOL)
    assert parked_steps[3].payload == PARKED

    assert job.status is S.SUCCEEDED
    assert [(step.kind, step.lease_epoch) for step in steps] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.APPROVAL_REQUESTED, 1),
        (K.STATUS_CHANGED, 1),
        (K.APPROVAL_GIVEN, 1),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.APPROVAL_REQUESTED, 2),
        (K.STATUS_CHANGED, 2),
        (K.APPROVAL_GIVEN, 2),
        (K.TOOL_INTENT, 3),
        (K.TOOL_RESULT, 3),
        (K.MODEL_REQUEST, 3),
        (K.MODEL_RESPONSE, 3),
    ]
    assert [steps[i].payload for i in (3, 8)] == [PARKED, PARKED]
    a, b = op(job, "toolu_a"), op(job, "toolu_b")
    # Each answer names its question by the question's `seq`.
    assert steps[4].payload == {"tool_use_id": "toolu_a", "operation_id": a, "question": 3}
    assert steps[9].payload == {"tool_use_id": "toolu_b", "operation_id": b, "question": 8}
    # One run each, by the tool's own record.
    assert acts.runs == [(a, "a"), (b, "b")]
    # A claim after an approval is an ordinary claim, not a takeover.
    assert (job.lease_epoch, job.takeovers) == (3, 0)
    # The model sees results like any others, and nothing of the approval.
    assert person_saw(steps) == ["did a", "did b"]


def person_saw(steps: list[Step]) -> list[str]:
    return [step.payload["content"] for step in steps if step.kind is K.TOOL_RESULT]


def test_a_denied_call_never_runs_and_the_job_goes_on(stack: Stack) -> None:
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step], list[list[dict[str, Any]]]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        await person.deny()
        # The next worker stores the refusal and goes on to the second call.
        assert await person.work() is S.NEEDS_APPROVAL
        assert acts.runs == []
        await person.approve()
        assert await person.work() is S.SUCCEEDED
        return *(await person.read()), person.script.requests

    job, steps, requests = with_person(stack, acts, body)
    assert job.status is S.SUCCEEDED
    assert kinds(steps) == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_DENIED,
        # No intent: the denied call has a result and never had a run.
        K.TOOL_RESULT,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
    ]
    a, b = op(job, "toolu_a"), op(job, "toolu_b")
    assert [steps[i].payload for i in (3, 7)] == [PARKED, PARKED]
    assert steps[4].payload == {"tool_use_id": "toolu_a", "operation_id": a, "question": 3}
    assert steps[5].payload == {
        "tool_use_id": "toolu_a",
        "content": DENIED,
        "is_error": True,
        "denied": True,
    }
    # The denied call never ran, by the tool's own record.
    assert acts.runs == [(b, "b")]
    # The model is told, in the place of the call's result.
    assert requests[-1][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a", "content": DENIED, "is_error": True},
            {
                "type": "tool_result",
                "tool_use_id": "toolu_b",
                "content": "did b",
                "is_error": False,
            },
        ],
    }


def test_a_spec_adds_a_tool_to_the_gate(stack: Stack) -> None:
    acts = Acts()

    async def body(person: Person) -> tuple[Job, list[Step]]:
        await person.submit({**SPEC, "needs_approval": ["act"]})
        assert await person.work() is S.NEEDS_APPROVAL
        return await person.read()

    job, steps = with_person(stack, acts, body)
    assert kinds(steps) == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
    ]
    assert steps[2].payload == question(job, "toolu_a", "a", BY_SPEC)
    assert steps[3].payload == PARKED
    assert acts.runs == []


@pytest.mark.parametrize("named", [[], ["read_file"]], ids=["empty-list", "another-tool"])
def test_a_spec_cannot_take_a_tool_out_of_the_gate(stack: Stack, named: list[str]) -> None:
    """A spec's list only adds. What the tool's definition says stays."""
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step]]:
        # `read_file` is not a tool of this worker's registry in the second
        # case, which fails the job before anything runs: also not a way round.
        await person.submit({**SPEC, "needs_approval": named})
        await person.work()
        return await person.read()

    job, steps = with_person(stack, acts, body)
    assert acts.runs == []
    if named:
        assert job.status is S.FAILED
    else:
        assert job.status is S.NEEDS_APPROVAL
        assert kinds(steps)[-2:] == [K.APPROVAL_REQUESTED, K.STATUS_CHANGED]
        assert steps[-2].payload == question(job, "toolu_a", "a", BY_TOOL)
        assert steps[-1].payload == PARKED


def test_a_spec_that_names_a_tool_the_worker_does_not_offer_fails_the_job(stack: Stack) -> None:
    """A misspelt name would gate nothing, and the tool it meant would run unasked."""
    acts = Acts()

    async def body(person: Person) -> tuple[Job, list[Step], int]:
        await person.submit({**SPEC, "needs_approval": ["act", "akt"]})
        assert await person.work() is S.FAILED
        return *(await person.read()), len(person.script.requests)

    job, steps, requests = with_person(stack, acts, body)
    assert job.error is not None
    assert job.error == (
        "the spec asks for approval before tools this worker does not offer: 'akt'"
    )
    assert kinds(steps) == [K.ERROR]
    # Nothing was asked of the model and nothing ran.
    assert requests == 0
    assert acts.runs == []


# One approval covers one run


@pytest.mark.parametrize(
    ("idempotent", "check_answer"),
    [(True, None), (False, CheckOutcome.NOT_DONE)],
    ids=["idempotent", "check-says-not-done"],
)
def test_an_approved_call_that_is_interrupted_is_asked_about_again(
    stack: Stack, idempotent: bool, check_answer: CheckOutcome | None
) -> None:
    """Without the gate this tool's interrupted call would simply be run again."""
    acts = Acts(gated=True, idempotent=idempotent, check_answer=check_answer)

    async def body(person: Person) -> tuple[Job, list[Step]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        await person.approve()
        await person.abandon_in_the_tool()
        assert len(acts.runs) == 1
        assert await person.work(takeover=True) is S.NEEDS_APPROVAL
        assert len(acts.runs) == 1
        # Asked again because the call was interrupted, and the row says so.
        assert await person.notices() == [(3, "before_run"), (7, "interrupted")]
        # The second approval lets it run again, under an intent of its own.
        await person.approve()
        assert await person.work() is S.NEEDS_APPROVAL
        return await person.read()

    job, steps = with_person(stack, acts, body)
    assert kinds(steps)[2:] == [
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        # The second call of the response.
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
    ]
    assert "each run of this tool needs a person's approval" in steps[6].payload["reason"]
    a = op(job, "toolu_a")
    # Two runs of the first call, each after an approval of its own.
    assert acts.runs == [(a, "a"), (a, "a")]


def test_a_denied_rerun_of_an_interrupted_call_does_not_run_and_the_job_goes_on(
    stack: Stack,
) -> None:
    """The question of an interrupted call (not a gated one) can be answered with no."""
    acts = Acts()

    async def body(person: Person) -> tuple[Job, list[Step]]:
        await person.submit()
        await person.abandon_in_the_tool()
        assert await person.work(takeover=True) is S.NEEDS_APPROVAL
        await person.deny()
        assert await person.work() is S.SUCCEEDED
        return await person.read()

    job, steps = with_person(stack, acts, body)
    assert kinds(steps) == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_DENIED,
        K.TOOL_RESULT,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
    ]
    assert steps[4].payload == PARKED
    assert steps[6].payload == {
        "tool_use_id": "toolu_a",
        "content": DENIED_RERUN,
        "is_error": True,
        "denied": True,
    }
    # The interrupted run, and no second one.
    assert acts.runs == [(op(job, "toolu_a"), "a"), (op(job, "toolu_b"), "b")]


# An answer names its question (ADR 0012)


def test_an_approval_sent_again_after_the_job_asked_about_the_next_call_is_refused(
    stack: Stack,
) -> None:
    """A retry of an approval whose response was lost, or a second person who
    approves the same question a moment later. The job has run the first call
    and asks about the second: the late answer must not let that one run."""
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step], Job, list[Step], OtherQuestion]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        first = await person.asked()
        await person.approve(first)
        assert await person.work() is S.NEEDS_APPROVAL
        before = await person.read()
        with pytest.raises(OtherQuestion) as refused:
            await person.approve(first)
        after = await person.read()
        # Nothing was written and nothing can be claimed: it still waits.
        async with person.engine.begin() as conn:
            assert await claim_job(conn, worker_id="late") is None
        return *before, *after, refused.value

    before_job, before_steps, job, steps, refused = with_person(stack, acts, body)
    assert (job.status, job.lease_epoch) == (S.NEEDS_APPROVAL, before_job.lease_epoch)
    assert steps == before_steps
    second, parked = steps[-2], steps[-1]
    assert (second.kind, second.payload["tool_use_id"]) == (K.APPROVAL_REQUESTED, "toolu_b")
    assert (parked.kind, parked.payload) == (K.STATUS_CHANGED, PARKED)
    assert (refused.question, refused.waiting_on) == (3, second.seq)
    # The first call ran once. The second call's tool has not run.
    assert acts.runs == [(op(job, "toolu_a"), "a")]


def test_an_approval_sent_again_after_the_same_call_was_asked_about_again_is_refused(
    stack: Stack,
) -> None:
    """The approved run is interrupted, and the call is asked about again: same
    call, same operation id, a new question. An answer to the first question
    must not approve a second run of a call that may already have run."""
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step], OtherQuestion, int]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        first = await person.asked()
        await person.approve(first)
        await person.abandon_in_the_tool()
        assert await person.work(takeover=True) is S.NEEDS_APPROVAL
        again = await person.asked()
        with pytest.raises(OtherQuestion) as refused:
            await person.approve(first)
        return *(await person.read()), refused.value, again

    job, steps, refused, again = with_person(stack, acts, body)
    assert job.status is S.NEEDS_APPROVAL
    questions = [step for step in steps if step.kind is K.APPROVAL_REQUESTED]
    assert [step.seq for step in questions] == [3, again]
    # The same call both times: the operation id cannot tell the questions apart.
    assert {step.payload["operation_id"] for step in questions} == {op(job, "toolu_a")}
    assert (refused.question, refused.waiting_on) == (3, again)
    assert kinds(steps)[-2:] == [K.APPROVAL_REQUESTED, K.STATUS_CHANGED]
    assert steps[-1].payload == PARKED
    # The interrupted run, and no second one.
    assert acts.runs == [(op(job, "toolu_a"), "a")]


def test_an_approval_that_names_no_question_does_not_count(stack: Stack) -> None:
    """As an approval written before answers named their question would be. The job
    is not taken to be answered: it waits on its question again."""
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[JobStatus | None, list[Step]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        async with person.engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET status = 'queued' WHERE id = :id"), {"id": person.job_id}
            )
            await conn.execute(
                text(
                    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
                    " VALUES (:id, 5, 'approval_given', CAST(:payload AS jsonb), 1)"
                ),
                {"id": person.job_id, "payload": '{"tool_use_id": "toolu_a"}'},
            )
        status = await person.work()
        return status, (await person.read())[1]

    status, steps = with_person(stack, acts, body)
    assert status is S.NEEDS_APPROVAL
    # No intent, no run, and no second question: the first is still open.
    # The job is parked on it again.
    assert kinds(steps) == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_GIVEN,
        K.STATUS_CHANGED,
    ]
    assert [steps[i].payload for i in (3, 5)] == [PARKED, PARKED]
    assert acts.runs == []


def test_a_denial_written_before_answers_named_their_question_still_holds(stack: Stack) -> None:
    """An interrupted call of a tool that is not gated, whose check could not tell.
    A person denied the rerun with an answer that names no question, as before
    answers named their question. Then the check says the call did not happen.
    Without the denial that would run it again."""
    acts = Acts(check_answer=CheckOutcome.UNKNOWN)

    async def body(person: Person) -> tuple[Job, list[Step]]:
        await person.submit()
        await person.abandon_in_the_tool()
        assert await person.work(takeover=True) is S.NEEDS_APPROVAL
        asked = await person.asked()
        async with person.engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET status = 'queued' WHERE id = :id"), {"id": person.job_id}
            )
            await conn.execute(
                text(
                    "INSERT INTO steps (job_id, seq, kind, payload, lease_epoch)"
                    " VALUES (:id, :seq, 'approval_denied', CAST(:payload AS jsonb), 2)"
                ),
                # After the question and the park's `status_changed` step.
                {"id": person.job_id, "seq": asked + 2, "payload": '{"tool_use_id": "toolu_a"}'},
            )
        acts.check_answer = CheckOutcome.NOT_DONE
        assert await person.work() is S.SUCCEEDED
        return await person.read()

    job, steps = with_person(stack, acts, body)
    a = op(job, "toolu_a")
    results = [step.payload for step in steps if step.kind is K.TOOL_RESULT]
    assert results[0] == {
        "tool_use_id": "toolu_a",
        "content": DENIED_RERUN,
        "is_error": True,
        "denied": True,
    }
    # The interrupted run, and no rerun. The second call is not gated and runs.
    assert acts.runs == [(a, "a"), (op(job, "toolu_b"), "b")]


# Workers that stop around the question


def test_a_question_asked_before_the_worker_stopped_is_not_asked_twice(stack: Stack) -> None:
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[JobStatus | None, list[Step]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        # As if the worker had stopped after asking and before parking the job.
        async with person.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET status = 'running', lease_owner = 'gone',"
                    " lease_expires_at = now() WHERE id = :id"
                ),
                {"id": person.job_id},
            )
        status = await person.work(takeover=True)
        return status, (await person.read())[1]

    status, steps = with_person(stack, acts, body)
    assert status is S.NEEDS_APPROVAL
    # One question. Each worker that parked the job on it wrote its move.
    assert [(step.kind, step.lease_epoch) for step in steps] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.APPROVAL_REQUESTED, 1),
        (K.STATUS_CHANGED, 1),
        (K.STATUS_CHANGED, 2),
    ]
    assert [steps[i].payload for i in (3, 4)] == [PARKED, PARKED]
    assert acts.runs == []


def test_a_job_parked_by_the_next_worker_on_a_question_asked_before_gets_its_row(
    stack: Stack,
) -> None:
    """A worker stopped between writing the question and the park. The next
    worker parks the job on that question, and its park writes the row."""
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[JobStatus | None, list[tuple[int, str]]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        # The park never happened: no move, no row. Only the owner may remove one.
        owner = await asyncpg.connect(as_owner(stack.database_url), timeout=5)
        try:
            await owner.execute("DELETE FROM notifications WHERE job_id = $1", person.job_id)
            await owner.execute(
                "UPDATE jobs SET status = 'running', lease_owner = 'gone',"
                " lease_expires_at = now() WHERE id = $1",
                person.job_id,
            )
        finally:
            await owner.close()
        status = await person.work(takeover=True)
        return status, await person.notices()

    status, found = with_person(stack, acts, body)
    assert status is S.NEEDS_APPROVAL
    # The question of the first worker, seq 3. Not asked again.
    assert found == [(3, "before_run")]
    assert acts.runs == []


def test_an_answer_about_another_call_does_not_let_this_one_run(stack: Stack) -> None:
    """The log is written by hand: an approval that names the second call while
    the first is the one that is open. The worker asks about the first."""
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step]]:
        await person.submit()
        async with person.engine.begin() as conn:
            claim = await claim_job(conn, worker_id="w1")
        assert claim is not None
        usage = {"input_tokens": 1, "output_tokens": 1}
        log: list[tuple[StepKind, dict[str, Any]]] = [
            (K.MODEL_REQUEST, {"model": "m", "max_tokens": 10, "messages": 1}),
            (
                K.MODEL_RESPONSE,
                {"id": "msg_0", "content": TURNS[0], "stop_reason": "tool_use", "usage": usage},
            ),
            (K.APPROVAL_REQUESTED, {"tool_use_id": "toolu_b", "name": "act"}),
            (K.APPROVAL_GIVEN, {"tool_use_id": "toolu_b", "question": 3}),
        ]
        for seq, (kind, payload) in enumerate(log, start=1):
            await held.append_step(
                person.engine, job_id=claim.id, lease_epoch=1, seq=seq, kind=kind, payload=payload
            )
        status = await run_job(
            person.engine, person.script, acts.registry(), claim, stack.settings()
        )
        assert status is S.NEEDS_APPROVAL
        return await person.read()

    job, steps = with_person(stack, acts, body)
    assert acts.runs == []
    assert kinds(steps)[-2:] == [K.APPROVAL_REQUESTED, K.STATUS_CHANGED]
    assert steps[-2].payload == question(job, "toolu_a", "a", BY_TOOL)
    assert steps[-1].payload == PARKED


# Workers that do not agree about the tool, as during a deploy that changes its definition


def test_an_open_question_holds_on_a_worker_whose_tool_would_not_have_asked(stack: Stack) -> None:
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[JobStatus | None, list[Step]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        # The worker stopped after asking and before parking the job, and
        # the one that takes over has the tool without the flag.
        async with person.engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET status = 'running', lease_owner = 'gone',"
                    " lease_expires_at = now() WHERE id = :id"
                ),
                {"id": person.job_id},
            )
        acts.gated = False
        status = await person.work(takeover=True)
        return status, (await person.read())[1]

    status, steps = with_person(stack, acts, body)
    assert status is S.NEEDS_APPROVAL
    # Not asked again: the new worker only parks the job on the open question.
    assert [(step.kind, step.lease_epoch) for step in steps] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.APPROVAL_REQUESTED, 1),
        (K.STATUS_CHANGED, 1),
        (K.STATUS_CHANGED, 2),
    ]
    assert [steps[i].payload for i in (3, 4)] == [PARKED, PARKED]
    assert acts.runs == []


def test_an_approved_run_is_marked_so_that_any_worker_asks_again_after_it_is_interrupted(
    stack: Stack,
) -> None:
    """The worker that takes over has the tool idempotent and without the flag.
    By its own tools it would simply run the call again, on the one approval."""
    acts = Acts(gated=True, idempotent=True)

    async def body(person: Person) -> tuple[Job, list[Step]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        await person.approve()
        await person.abandon_in_the_tool()
        acts.gated = False
        assert await person.work(takeover=True) is S.NEEDS_APPROVAL
        assert len(acts.runs) == 1
        # Approved again, also by a worker without the flag: the rerun is marked too.
        await person.approve()
        await person.abandon_in_the_tool()
        assert await person.work(takeover=True) is S.NEEDS_APPROVAL
        return await person.read()

    _, steps = with_person(stack, acts, body)
    assert kinds(steps)[2:] == [
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
    ]
    assert [step.payload.get("needs_approval") for step in steps if step.kind is K.TOOL_INTENT] == [
        True,
        True,
    ]
    assert len(acts.runs) == 2


def test_an_intent_of_a_call_nobody_was_asked_about_is_not_marked(stack: Stack) -> None:
    acts = Acts()

    async def body(person: Person) -> list[Step]:
        await person.submit()
        assert await person.work() is S.SUCCEEDED
        return (await person.read())[1]

    steps = with_person(stack, acts, body)
    intents = [step.payload for step in steps if step.kind is K.TOOL_INTENT]
    assert len(intents) == 2
    assert all("needs_approval" not in intent for intent in intents)


# Nobody answers


def test_a_job_nobody_answers_is_failed_at_its_deadline_and_the_call_never_runs(
    stack: Stack,
) -> None:
    acts = Acts(gated=True)

    async def body(person: Person) -> tuple[Job, list[Step], list[Any]]:
        await person.submit()
        assert await person.work() is S.NEEDS_APPROVAL
        async with person.engine.begin() as conn:
            # Not yet at its deadline: left alone.
            assert await fail_overdue(conn) == []
            await conn.execute(
                text("UPDATE jobs SET deadline = clock_timestamp() WHERE id = :id"),
                {"id": person.job_id},
            )
        async with person.engine.begin() as conn:
            ended = await fail_overdue(conn)
        # Too late for an answer, and nothing is left to claim.
        refused = []
        asked = await person.asked()
        for answer in (approve_job, deny_job):
            async with person.engine.begin() as conn:
                try:
                    await answer(conn, person.job_id, asked)
                except InvalidTransition as error:
                    refused.append(error)
        async with person.engine.begin() as conn:
            assert await claim_job(conn, worker_id="late") is None
        assert [job.id for job in ended] == [person.job_id]
        return *(await person.read()), refused

    job, steps, refused = with_person(stack, acts, body)
    assert (job.status, job.error) == (S.FAILED, DEADLINE_ERROR)
    assert kinds(steps) == [
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
        K.APPROVAL_REQUESTED,
        K.STATUS_CHANGED,
        K.ERROR,
    ]
    assert steps[3].payload == PARKED
    assert len(refused) == 2
    assert acts.runs == []
