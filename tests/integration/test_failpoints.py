"""Each failpoint is reached by the workload job, and a second worker gets the job past it.

The job is the CI triage workload: read a log, open an issue. It runs against
the compose stack's fake provider and fake tracker over HTTP, and a real
Postgres. The tracker's own list of issues is the evidence for what happened
outside the runtime.

The first worker stops at the failpoint. Stopping is an exception that
nothing in the worker catches, which leaves the database as a killed process
would. The second worker has the same failpoint switched on and must not stop
at it, because the job has been taken over.
"""

import asyncio
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind, read_steps
from runtime.tools import default_tools
from runtime.worker.agent import AgentSettings, run_job
from runtime.worker.failpoints import NAMES, Failpoints

from .conftest import Stack, expire_lease

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


def test_every_failpoint_has_an_expectation() -> None:
    assert tuple(EXPECTED) == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_a_job_stopped_at_the_failpoint_is_finished_by_a_second_worker_with_one_issue(
    stack: Stack, fake_tracker_url: str, name: str
) -> None:
    tools = default_tools(tracker_url=fake_tracker_url)

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
