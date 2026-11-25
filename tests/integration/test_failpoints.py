"""Each failpoint is reached by the workload job, and a second worker gets the job past it.

The job is the CI triage workload: read a log, open an issue. It runs against
the compose stack's fake provider and fake tracker over HTTP, and a real
Postgres. The tracker's own list of issues is the evidence for what happened
outside the runtime.

The first worker stops at the failpoint. Stopping is an exception that
nothing in the worker catches, which leaves the database as a killed process
would. The second worker has the same failpoint switched on and must not stop
at it, because the job has been taken over.

The last tests are about a job that summarises: the notes job of
`test_summary.py`, which stores two summaries. Its two failpoints stop a
worker right after the job's first summary is stored, and right after a
summary request is written and before it is sent.
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.failpoints import Failpoints
from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.tools.trackers import FakeTracker
from runtime.worker import agent
from runtime.worker.agent import AgentSettings, run_job
from runtime.worker.replay import Replay, replay

from .conftest import Stack, expire_lease
from .test_summary import SPEC as NOTES_JOB
from .test_summary import SUMMARIES, TRIGGER, of_kind, summary_requests
from .test_summary import TOOLS as NOTES_TOOLS

K = StepKind
SPEC = {
    "prompt": "The CI run failed. Read ci.log and open an issue.",
    "model": "ci-triage",
    "files": {"ci.log": "FAILED tests/test_checkout.py::test_checkout_total\n"},
}
# The step log of the job when nobody interrupts it.
WHOLE_JOB = [
    K.MODEL_REQUEST,
    K.MODEL_RESPONSE,
    K.TOOL_INTENT,
    K.TOOL_RESULT,
    K.MODEL_REQUEST,
    K.MODEL_RESPONSE,
    K.TOOL_INTENT,
    K.TOOL_RESULT,
    K.MODEL_REQUEST,
    K.MODEL_RESPONSE,
]


class Stopped(BaseException):
    """The worker process ended at a failpoint."""


def stop() -> None:
    raise Stopped


# What the first worker leaves in the log when it stops at each failpoint
# (the tool failpoints are limited to create_issue), and what the second
# worker adds.
EXPECTED: dict[str, tuple[list[StepKind], list[StepKind]]] = {
    "after_claim": ([], WHOLE_JOB),
    "after_model_request": ([K.MODEL_REQUEST], WHOLE_JOB),
    "mid_stream": ([K.MODEL_REQUEST], WHOLE_JOB),
    # The check finds no issue: the call is run again under a new intent.
    "after_tool_intent": (WHOLE_JOB[:7], [K.TOOL_INTENT, K.TOOL_RESULT, *WHOLE_JOB[8:]]),
    # The check finds the issue: the result is recovered, the tool does not run again.
    "after_tool_ran": (WHOLE_JOB[:7], [K.TOOL_RESULT, *WHOLE_JOB[8:]]),
    "after_tool_result": (WHOLE_JOB[:8], WHOLE_JOB[8:]),
}


# The failpoints a job with no summary passes. The workload job runs with no
# trigger and never reaches `after_summary`, which has its own tests below.
WITHOUT_A_SUMMARY = (
    "after_claim",
    "after_model_request",
    "mid_stream",
    "after_tool_intent",
    "after_tool_ran",
    "after_tool_result",
)


def test_every_failpoint_has_an_expectation() -> None:
    assert tuple(EXPECTED) == WITHOUT_A_SUMMARY


@pytest.mark.parametrize("name", WITHOUT_A_SUMMARY)
def test_a_job_stopped_at_the_failpoint_is_finished_by_a_second_worker_with_one_issue(
    stack: Stack, fake_tracker_url: str, name: str
) -> None:
    tools = default_tools(tracker=FakeTracker(fake_tracker_url))

    def settings() -> AgentSettings:
        plain = stack.settings()
        return AgentSettings(
            default_model=plain.default_model,
            max_tokens=plain.max_tokens,
            workspace_root=plain.workspace_root,
            failpoints=Failpoints(name, tool="create_issue", crash=stop),
        )

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> dict[str, Any]:
        async with engine.begin() as conn:
            await stack.create(conn, SPEC)
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        with pytest.raises(Stopped):
            await run_job(engine, provider, tools, first_claim, settings())
        async with engine.begin() as conn:
            left = await read_steps(conn, first_claim.id)
            abandoned = await get_job(conn, first_claim.id)
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        status = await run_job(engine, provider, tools, second_claim, settings())
        async with engine.begin() as conn:
            job = await get_job(conn, first_claim.id)
            steps = await read_steps(conn, first_claim.id)
        return {
            "abandoned": abandoned,
            "left": left,
            "second_claim": second_claim,
            "status": status,
            "job": job,
            "steps": steps,
        }

    seen = stack.run(body)
    left_expected, added_expected = EXPECTED[name]
    left: list[Step] = seen["left"]
    steps: list[Step] = seen["steps"]
    job: Job = seen["job"]

    # The first worker stopped exactly at the failpoint, with the job still its own.
    assert [step.kind for step in left] == left_expected
    assert seen["abandoned"].status is JobStatus.RUNNING
    # The second worker passed the same point and finished the job.
    assert (seen["second_claim"].lease_epoch, seen["second_claim"].takeovers) == (2, 1)
    assert seen["status"] is JobStatus.SUCCEEDED
    assert job.status is JobStatus.SUCCEEDED
    assert [step.kind for step in steps[len(left) :]] == added_expected
    assert {step.lease_epoch for step in steps[len(left) :]} == {2}

    # The tracker: one issue for this job, whatever happened in between.
    issues = httpx2.get(f"{fake_tracker_url}/issues", timeout=10).json()
    ours = [issue for issue in issues if issue["operation_id"].startswith(f"{job.id}:")]
    assert len(ours) == 1
    create_results = [
        step.payload
        for step in steps
        if step.kind is K.TOOL_RESULT and "created issue" in step.payload["content"]
    ]
    assert len(create_results) == 1
    assert create_results[0]["content"].startswith(f"created issue {ours[0]['id']}:")
    assert create_results[0].get("recovered", False) is (name == "after_tool_ran")

    # The provider's log: the last request is the same conversation as in a
    # job that nobody interrupted, apart from the issue number.
    final_request = stack.requests_for(job)[-1]["body"]["messages"]
    assert [message["role"] for message in final_request] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert final_request[4]["content"][0]["content"] == create_results[0]["content"]


def test_the_mid_stream_failpoint_fires_after_the_answer_began_and_before_it_ended(
    stack: Stack,
) -> None:
    """The provider's log shows the request arrived. The worker has no response step."""

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, {**SPEC, "model": "ci-triage-slow"})
            claim = await claim_job(conn, worker_id="w1")
        assert claim is not None
        plain = stack.settings()
        settings = AgentSettings(
            default_model=plain.default_model,
            max_tokens=plain.max_tokens,
            workspace_root=plain.workspace_root,
            failpoints=Failpoints("mid_stream", crash=stop),
        )
        started = asyncio.get_running_loop().time()
        with pytest.raises(Stopped):
            await run_job(engine, provider, default_tools(), claim, settings)
        # The slow scenario waits half a second inside the stream, before
        # the first content block. The failpoint fired after that wait.
        assert asyncio.get_running_loop().time() - started >= 0.5
        async with engine.begin() as conn:
            return claim, await read_steps(conn, claim.id)

    job, steps = stack.run(body)
    assert [step.kind for step in steps] == [K.MODEL_REQUEST]
    assert [(r["status"], r["turn"]) for r in stack.requests_for(job)] == [(200, 0)]


# A job that summarises


class Counting:
    """What a failpoint does when it fires: stop the worker, and count it."""

    def __init__(self) -> None:
        self.fired = 0

    def __call__(self) -> None:
        self.fired += 1
        raise Stopped


@dataclass
class Split:
    # The claim the first worker ran on, and the log it left when it stopped.
    first_claim: Job
    left: list[Step]
    # The claim of the worker that took over, and the status it left the job in.
    second_claim: Job
    status: JobStatus | None
    job: Job
    steps: list[Step]
    # How often the failpoint stopped a worker, over both.
    fired: int
    # The fake provider's log of the job's requests.
    requests: list[dict[str, Any]]

    @property
    def added(self) -> list[Step]:
        """The steps of the worker that took over."""
        return self.steps[len(self.left) :]


def stopped_then_taken_over(
    stack: Stack,
    points: Callable[[Callable[[], None]], Failpoints],
    *,
    lease_lost_first: bool,
    spec: dict[str, Any] = NOTES_JOB,
    trigger: int = TRIGGER,
) -> Split:
    """The notes job: a worker stops at the failpoint, and another takes the job over.

    Both workers have the failpoint switched on. With `lease_lost_first`, a
    worker that did nothing held the job before the first one.
    """
    stopping = Counting()

    def settings() -> AgentSettings:
        return replace(stack.settings(summary_trigger=trigger), failpoints=points(stopping))

    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> Split:
        async with engine.begin() as conn:
            await stack.create(conn, spec)
            first_claim = await claim_job(conn, worker_id="w1")
            assert first_claim is not None
            if lease_lost_first:
                await expire_lease(conn, first_claim.id)
                first_claim = await claim_job(conn, worker_id="w2")
                assert first_claim is not None
        with pytest.raises(Stopped):
            await run_job(engine, provider, NOTES_TOOLS, first_claim, settings())
        async with engine.begin() as conn:
            left = await read_steps(conn, first_claim.id)
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w3")
        assert second_claim is not None
        status = await run_job(engine, provider, NOTES_TOOLS, second_claim, settings())
        async with engine.begin() as conn:
            job = await get_job(conn, first_claim.id)
            steps = await read_steps(conn, first_claim.id)
        assert job is not None
        return Split(first_claim, left, second_claim, status, job, steps, stopping.fired, [])

    split = stack.run(body)
    split.requests = stack.requests_for(split.job)
    return split


def after_summary(stop: Callable[[], None]) -> Failpoints:
    return Failpoints("after_summary", crash=stop)


def test_a_worker_stops_after_the_first_summary_and_the_next_worker_finishes_the_job(
    stack: Stack,
) -> None:
    """`after_summary` fires once in the job, at its first summary.

    The worker that takes over has it switched on too. It stores the second
    summary and goes on.
    """
    split = stopped_then_taken_over(stack, after_summary, lease_lost_first=False)

    # The first worker stopped with the first summary as its last step.
    assert split.left[-1].kind is K.SUMMARY
    assert len(of_kind(split.left, K.SUMMARY)) == 1
    assert split.first_claim.takeovers == 0
    # The second worker stored the other summary and finished the job.
    assert split.second_claim.takeovers == 1
    assert split.status is JobStatus.SUCCEEDED
    assert split.job.status is JobStatus.SUCCEEDED
    assert len(of_kind(split.steps, K.SUMMARY)) == SUMMARIES
    assert {step.lease_epoch for step in split.added} == {2}
    assert split.fired == 1


def test_a_job_taken_over_before_any_summary_still_stops_at_its_first_summary(
    stack: Stack,
) -> None:
    """A lease lost before the first summary does not cancel `after_summary`."""
    split = stopped_then_taken_over(stack, after_summary, lease_lost_first=True)

    # The worker that stopped ran a job that had been taken over once.
    assert (split.first_claim.lease_epoch, split.first_claim.takeovers) == (2, 1)
    assert split.left[-1].kind is K.SUMMARY
    assert len(of_kind(split.left, K.SUMMARY)) == 1
    assert split.status is JobStatus.SUCCEEDED
    assert len(of_kind(split.steps, K.SUMMARY)) == SUMMARIES
    assert split.fired == 1


def test_the_first_summary_is_counted_from_the_log_and_not_from_the_replayed_state(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a replay that skips `summary` steps, the next worker still gets past the failpoint.

    That worker rebuilds the conversation without the summary, so it asks
    for one again and stores a second. The log then holds two, and the
    failpoint, which reads the log, does not stop it.
    """

    def skipping(prompt: str, steps: Iterable[Step]) -> Replay:
        return replay(prompt, [step for step in steps if step.kind is not K.SUMMARY])

    # The worker loop's replay only. Its step writer folds a new step in as before.
    monkeypatch.setattr(agent, "replay", skipping)
    split = stopped_then_taken_over(stack, after_summary, lease_lost_first=False)

    assert split.left[-1].kind is K.SUMMARY
    assert len(of_kind(split.left, K.SUMMARY)) == 1
    # The second worker's first two steps: a summary request and the job's
    # second summary. It did not stop there, and went on to the job's end.
    assert [step.kind for step in split.added[:2]] == [K.MODEL_REQUEST, K.SUMMARY]
    assert split.added[0].payload.get("summary") is True
    assert {step.lease_epoch for step in split.added} == {2}
    assert split.status is JobStatus.SUCCEEDED
    # One summary more than the job has when its replay reads them.
    assert len(of_kind(split.steps, K.SUMMARY)) == SUMMARIES + 1
    assert split.fired == 1


def test_a_worker_stops_at_a_summary_request_before_it_is_sent_and_the_next_finishes(
    stack: Stack,
) -> None:
    """`after_model_request` limited to a summary request passes the requests before it."""
    split = stopped_then_taken_over(
        stack,
        lambda stop: Failpoints("after_model_request", summary=True, crash=stop),
        lease_lost_first=False,
    )

    # The first worker's last step is a summary request, not the job's first request.
    requests = of_kind(split.left, K.MODEL_REQUEST)
    assert split.left[-1].kind is K.MODEL_REQUEST
    assert split.left[-1].payload.get("summary") is True
    assert len(requests) > 1
    assert "summary" not in requests[0].payload
    assert of_kind(split.left, K.SUMMARY) == []
    # The second worker sent it and finished the job.
    assert split.second_claim.takeovers == 1
    assert split.status is JobStatus.SUCCEEDED
    assert len(of_kind(split.steps, K.SUMMARY)) == SUMMARIES
    assert split.fired == 1
    # The request the first worker wrote never left it: the provider got
    # one summary request for each summary.
    assert len(summary_requests(split.requests)) == SUMMARIES


def test_a_summary_stored_with_a_problem_counts_as_the_jobs_first_summary(stack: Stack) -> None:
    """`after_summary` stops the worker after a summary that cannot be used too.

    The scenario answers the summary request with empty text. The worker
    stores that with its problem and stops. The next worker fails the job
    for the problem and asks for no other summary.
    """
    problem = "it has no text"
    split = stopped_then_taken_over(
        stack,
        after_summary,
        lease_lost_first=False,
        spec={**NOTES_JOB, "model": "summary-bad-empty"},
        trigger=100,
    )

    # The first worker stopped with the bad summary as its last step.
    assert split.left[-1].kind is K.SUMMARY
    assert split.left[-1].payload["problem"] == problem
    assert len(of_kind(split.left, K.SUMMARY)) == 1
    assert split.fired == 1
    # The second worker wrote the job's error and nothing else.
    assert split.status is JobStatus.FAILED
    assert split.job.error == f"the summary is not usable: {problem}"
    assert [step.kind for step in split.added] == [K.ERROR]
    assert len(summary_requests(split.requests)) == 1
