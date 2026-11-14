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

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store import held
from runtime.store.jobs import (
    DEADLINE_ERROR,
    InvalidTransition,
    Job,
    approve_job,
    claim_job,
    deny_job,
    fail_overdue,
    get_job,
)
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools.base import CheckOutcome
from runtime.worker.agent import run_job
from runtime.worker.approval import DENIED, DENIED_RERUN

from .conftest import Stack, expire_lease
from .test_resume import SPEC, TURNS, Acts, Script, op

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

    async def approve(self) -> None:
        async with self.engine.begin() as conn:
            await approve_job(conn, self.job_id)

    async def deny(self) -> None:
        async with self.engine.begin() as conn:
            await deny_job(conn, self.job_id)

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
        return *parked, *(await person.read())

    parked_job, parked_steps, job, steps = with_person(stack, acts, body)
    assert (parked_job.status, parked_job.lease_owner, parked_job.error) == (
        S.NEEDS_APPROVAL,
        None,
        None,
    )
    assert kinds(parked_steps) == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.APPROVAL_REQUESTED]
    assert parked_steps[2].payload == question(job, "toolu_a", "a", BY_TOOL)

    assert job.status is S.SUCCEEDED
    assert [(step.kind, step.lease_epoch) for step in steps] == [
        (K.MODEL_REQUEST, 1),
        (K.MODEL_RESPONSE, 1),
        (K.APPROVAL_REQUESTED, 1),
        (K.APPROVAL_GIVEN, 1),
        (K.TOOL_INTENT, 2),
        (K.TOOL_RESULT, 2),
        (K.APPROVAL_REQUESTED, 2),
        (K.APPROVAL_GIVEN, 2),
        (K.TOOL_INTENT, 3),
        (K.TOOL_RESULT, 3),
        (K.MODEL_REQUEST, 3),
        (K.MODEL_RESPONSE, 3),
    ]
    a, b = op(job, "toolu_a"), op(job, "toolu_b")
    assert steps[3].payload == {"tool_use_id": "toolu_a", "operation_id": a}
    assert steps[7].payload == {"tool_use_id": "toolu_b", "operation_id": b}
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
        K.APPROVAL_DENIED,
        # No intent: the denied call has a result and never had a run.
        K.TOOL_RESULT,
        K.APPROVAL_REQUESTED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
    ]
    a, b = op(job, "toolu_a"), op(job, "toolu_b")
    assert steps[3].payload == {"tool_use_id": "toolu_a", "operation_id": a}
    assert steps[4].payload == {
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
    assert kinds(steps) == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.APPROVAL_REQUESTED]
    assert steps[2].payload == question(job, "toolu_a", "a", BY_SPEC)
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
        assert steps[-1].payload == question(job, "toolu_a", "a", BY_TOOL)


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
        # The second approval lets it run again, under an intent of its own.
        await person.approve()
        assert await person.work() is S.NEEDS_APPROVAL
        return await person.read()

    job, steps = with_person(stack, acts, body)
    assert kinds(steps)[2:] == [
        K.APPROVAL_REQUESTED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.APPROVAL_REQUESTED,
        K.APPROVAL_GIVEN,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        # The second call of the response.
        K.APPROVAL_REQUESTED,
    ]
    assert "each run of this tool needs a person's approval" in steps[5].payload["reason"]
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
        K.APPROVAL_DENIED,
        K.TOOL_RESULT,
        K.TOOL_INTENT,
        K.TOOL_RESULT,
        K.MODEL_REQUEST,
        K.MODEL_RESPONSE,
    ]
    assert steps[5].payload == {
        "tool_use_id": "toolu_a",
        "content": DENIED_RERUN,
        "is_error": True,
        "denied": True,
    }
    # The interrupted run, and no second one.
    assert acts.runs == [(op(job, "toolu_a"), "a"), (op(job, "toolu_b"), "b")]


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
    assert kinds(steps) == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.APPROVAL_REQUESTED]
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
            (K.APPROVAL_GIVEN, {"tool_use_id": "toolu_b"}),
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
    assert steps[-1].kind is K.APPROVAL_REQUESTED
    assert steps[-1].payload == question(job, "toolu_a", "a", BY_TOOL)


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
        for answer in (approve_job, deny_job):
            async with person.engine.begin() as conn:
                try:
                    await answer(conn, person.job_id)
                except InvalidTransition as error:
                    refused.append(error)
        async with person.engine.begin() as conn:
            assert await claim_job(conn, worker_id="late") is None
        assert [job.id for job in ended] == [person.job_id]
        return *(await person.read()), refused

    job, steps, refused = with_person(stack, acts, body)
    assert (job.status, job.error) == (S.FAILED, DEADLINE_ERROR)
    assert kinds(steps) == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.APPROVAL_REQUESTED, K.ERROR]
    assert len(refused) == 2
    assert acts.runs == []
