"""A refusal by the model: what its step holds, what the job says, what is never shown.

Against real Postgres. The stack's fake provider scripts the ordinary cases.
Hostile values come through the real SDK from a stubbed transport, because a
scenario file cannot hold them.
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import asyncpg
import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fake_provider.messages import encode_sse, stream_events
from runtime.cli import format_step
from runtime.logs import JsonFormatter
from runtime.provider.messages import AnthropicProvider
from runtime.store.engine import create_engine
from runtime.store.jobs import Job, claim_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, read_steps
from runtime.store.steps import StepKind as K
from runtime.worker.__main__ import worker_engine
from runtime.worker.agent import _CUT, ERROR_TEXT_LENGTH, run_job

from .conftest import FIXED, HELLO, Stack, as_owner, expire_lease, on
from .test_api import Api, api_test

# The packaged scenarios: a refusal with details, and one without.
REFUSED = {"prompt": "Do the thing.", "model": "refusal"}
BARE = {"prompt": "Do the thing.", "model": "refusal-no-details"}
THINKING = {"prompt": "Think, then answer.", "model": "thinking"}
MARKER = "refusal-explanation-marker"
EXPLANATION = f"{MARKER}: the reason the provider gave for this refusal."
DECLINED = "the model declined the request"
WITH_CATEGORY = f"{DECLINED} (category: cyber)"
# The keys of a step of an ordinary answer, as they were before refusals had a field.
ORDINARY_KEYS = {"id", "content", "stop_reason", "usage"}
ORDINARY_USAGE = {"input_tokens", "output_tokens"}


def response_of(steps: list[Step]) -> dict[str, Any]:
    found = [step.payload for step in steps if step.kind is K.MODEL_RESPONSE]
    assert len(found) == 1
    return found[0]


def test_a_refused_job_stores_the_category_and_the_explanation(stack: Stack) -> None:
    ran = stack.run_one(REFUSED)

    assert ran.job.status is JobStatus.FAILED
    assert ran.kinds == [K.MODEL_REQUEST, K.MODEL_RESPONSE, K.ERROR]
    assert response_of(ran.steps)["refusal"] == {"category": "cyber", "explanation": EXPLANATION}
    assert ran.job.error == WITH_CATEGORY
    assert ran.steps[-1].payload == {"message": WITH_CATEGORY}


def test_a_refusal_without_details_has_null_fields_and_todays_error(stack: Stack) -> None:
    ran = stack.run_one(BARE)

    assert ran.job.status is JobStatus.FAILED
    assert response_of(ran.steps)["refusal"] == {"category": None, "explanation": None}
    assert ran.job.error == DECLINED


@pytest.mark.parametrize("spec", [REFUSED, BARE], ids=["with details", "without"])
def test_a_refusal_is_sent_once(stack: Stack, spec: dict[str, Any]) -> None:
    ran = stack.run_one(spec)

    assert len(ran.requests) == 1
    assert ran.requests[0]["status"] == 200
    assert ran.requests[0]["body"]["model"] == spec["model"]


async def _refuse_writes(conn: asyncpg.Connection[asyncpg.Record], kind: str) -> None:
    """A database that turns down one kind of step: a real failed write, no mock."""
    await conn.execute(
        "CREATE FUNCTION refuse_write() RETURNS trigger LANGUAGE plpgsql AS"
        " $$ BEGIN RAISE EXCEPTION 'refused in this test'; END $$"
    )
    await conn.execute(
        "CREATE TRIGGER refuse_write BEFORE INSERT ON steps FOR EACH ROW"
        f" WHEN (NEW.kind = '{kind}') EXECUTE FUNCTION refuse_write()"
    )


async def _allow_writes(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    await conn.execute("DROP TRIGGER refuse_write ON steps")
    await conn.execute("DROP FUNCTION refuse_write")


def test_a_takeover_after_the_refused_step_writes_the_same_error(stack: Stack) -> None:
    """The first worker stores the refused step and goes away before the error."""
    owner = as_owner(stack.database_url)
    on(owner, lambda conn: _refuse_writes(conn, "error"))

    async def first(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Any, list[Step]]:
        async with engine.begin() as conn:
            await stack.create(conn, REFUSED)
            claimed = await claim_job(conn, worker_id="first-worker")
        assert claimed is not None
        left = await run_job(engine, provider, FIXED, claimed, stack.settings())
        async with engine.begin() as conn:
            return (left, claimed.id), await read_steps(conn, claimed.id)

    (left, job_id), steps = stack.run(first)
    assert left is None
    assert [step.kind for step in steps] == [K.MODEL_REQUEST, K.MODEL_RESPONSE]
    on(owner, _allow_writes)

    async def second(engine: AsyncEngine, provider: AnthropicProvider) -> Job:
        async with engine.begin() as conn:
            await expire_lease(conn, job_id)
            claimed = await claim_job(conn, worker_id="second-worker")
        assert claimed is not None
        assert claimed.takeovers == 1
        await run_job(engine, provider, FIXED, claimed, stack.settings())
        async with engine.begin() as conn:
            job = await get_job(conn, job_id)
        assert job is not None
        return job

    job = stack.run(second)

    assert job.status is JobStatus.FAILED
    assert job.error == WITH_CATEGORY
    # The second worker read the refusal from the log and did not ask again.
    assert len(stack.requests_for(job)) == 1


@contextmanager
def log_records() -> Iterator[list[logging.LogRecord]]:
    """Every record that reaches the root logger, at any level."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = Keep(level=logging.DEBUG)
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


def as_worker(stack: Stack, spec: dict[str, Any]) -> Job:
    """Run one job on the engine the worker process uses."""

    async def go() -> Job:
        engine = worker_engine(stack.database_url)
        provider = AnthropicProvider(api_key="test", base_url=stack.fake_provider_url)
        try:
            async with engine.begin() as conn:
                await stack.create(conn, spec)
                claimed = await claim_job(conn, worker_id="test-worker")
            assert claimed is not None
            await run_job(engine, provider, FIXED, claimed, stack.settings())
            async with engine.begin() as conn:
                job = await get_job(conn, claimed.id)
            assert job is not None
            return job
        finally:
            await provider.aclose()
            await engine.dispose()

    return asyncio.run(go())


def logged(records: list[logging.LogRecord]) -> str:
    """The records as the worker writes them, with everything passed in `extra`."""
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in records)


def test_the_explanation_is_in_no_log_record(stack: Stack) -> None:
    with log_records() as records:
        job = as_worker(stack, REFUSED)

    assert job.error == WITH_CATEGORY
    text = logged(records)
    assert "job finished" in text
    assert WITH_CATEGORY in text
    assert MARKER not in text


def test_a_failed_write_of_the_step_does_not_log_the_explanation(stack: Stack) -> None:
    """SQLAlchemy puts a statement's parameters in its error text unless told not to."""
    owner = as_owner(stack.database_url)
    on(owner, lambda conn: _refuse_writes(conn, "model_response"))

    with log_records() as records:
        job = as_worker(stack, REFUSED)

    assert job.status is JobStatus.RUNNING
    text = logged(records)
    assert "the database could not be reached" in text
    assert "refused in this test" in text
    assert MARKER not in text


def test_the_explanation_is_in_the_step_and_nowhere_a_person_reads(stack: Stack) -> None:
    async def body(api: Api) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        async with api.engine.begin() as conn:
            await stack.create(conn, REFUSED)
            claimed = await claim_job(conn, worker_id="test-worker")
        assert claimed is not None
        provider = AnthropicProvider(api_key="test", base_url=stack.fake_provider_url)
        try:
            await run_job(api.engine, provider, FIXED, claimed, stack.settings())
        finally:
            await provider.aclose()
        page = await api.client.get("/")
        job = await api.client.get(f"/jobs/{claimed.id}")
        steps = await api.client.get(f"/jobs/{claimed.id}/steps")
        return page.text, job.json(), steps.json()["steps"]

    page, job, steps = api_test(stack.database_url, body)

    # The page has the job and its error, so the check below looks at the right text.
    assert WITH_CATEGORY in page
    assert MARKER not in page
    assert job["error"] == WITH_CATEGORY
    lines = [format_step(step) for step in steps]
    assert "refusal=cyber" in lines[1]
    assert MARKER not in "\n".join(lines)
    # It is kept: the step is where an operator with database or API access reads it.
    assert steps[1]["payload"]["refusal"]["explanation"] == EXPLANATION


# Hostile values from the provider

LEFT_OUT = object()
MESSAGE = {
    "id": "msg_hostile",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "No."}],
    "stop_reason": "refusal",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 9},
}


def run_hostile(
    stack: Stack, *, details: Any = LEFT_OUT, thinking: Any = LEFT_OUT
) -> tuple[Job, list[Step], int]:
    """One job against a provider that sends these values in its `message_delta`."""
    sent: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(1)
        chunks = []
        for event, data in stream_events(MESSAGE):
            if event == "message_delta":
                if details is not LEFT_OUT:
                    data["delta"]["stop_details"] = details
                if thinking is not LEFT_OUT:
                    data["usage"]["output_tokens_details"] = {"thinking_tokens": thinking}
            chunks.append(encode_sse(event, data))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=b"".join(chunks)
        )

    async def go() -> tuple[Job, list[Step]]:
        engine = create_engine(stack.database_url)
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        provider = AnthropicProvider(api_key="test", base_url="http://provider", http_client=http)
        try:
            async with engine.begin() as conn:
                await stack.create(conn, {"prompt": "go", "model": "m"})
                claimed = await claim_job(conn, worker_id="test-worker")
            assert claimed is not None
            await run_job(engine, provider, FIXED, claimed, stack.settings())
            async with engine.begin() as conn:
                job = await get_job(conn, claimed.id)
                assert job is not None
                return job, await read_steps(conn, claimed.id)
        finally:
            await provider.aclose()
            await engine.dispose()

    job, steps = asyncio.run(go())
    return job, steps, len(sent)


def refusal(category: Any = "cyber", explanation: Any = "why") -> dict[str, Any]:
    return {"type": "refusal", "category": category, "explanation": explanation}


@pytest.mark.parametrize(
    "category",
    [
        7,
        True,
        ["cyber"],
        {"a": 1},
        "",
        "c" * 65,
        "CYBER",
        "cyber\n",
        "cy\x1b[31mber",
        "a\x00",
        "\ud800",
    ],
)
def test_a_hostile_category_is_dropped_and_the_job_fails_as_a_refusal(
    stack: Stack, category: Any
) -> None:
    job, steps, sent = run_hostile(stack, details=refusal(category=category))

    assert job.status is JobStatus.FAILED
    assert job.error == DECLINED
    assert response_of(steps)["refusal"] == {"category": None, "explanation": "why"}
    assert sent == 1


@pytest.mark.parametrize("explanation", ["a\x00b", "a\ud800b", 7, ["why"], {"a": 1}])
def test_a_hostile_explanation_is_dropped_and_the_step_stores(
    stack: Stack, explanation: Any
) -> None:
    job, steps, sent = run_hostile(stack, details=refusal(explanation=explanation))

    assert job.status is JobStatus.FAILED
    assert job.error == WITH_CATEGORY
    assert response_of(steps)["refusal"] == {"category": "cyber", "explanation": None}
    assert sent == 1


def test_a_long_explanation_is_cut_to_what_a_step_keeps(stack: Stack) -> None:
    job, steps, _ = run_hostile(stack, details=refusal(explanation="e" * 200_000))

    stored = response_of(steps)["refusal"]["explanation"]
    assert job.error == WITH_CATEGORY
    assert len(stored) == ERROR_TEXT_LENGTH
    assert stored.endswith(_CUT)


def test_what_cannot_be_stored_past_the_cut_does_not_matter(stack: Stack) -> None:
    _, steps, _ = run_hostile(stack, details=refusal(explanation="e" * 2000 + "\x00"))

    assert len(response_of(steps)["refusal"]["explanation"]) == ERROR_TEXT_LENGTH


@pytest.mark.parametrize("details", ["nonsense", 5, [1], True], ids=str)
def test_details_of_another_shape_still_fail_as_a_refusal(stack: Stack, details: Any) -> None:
    job, steps, _ = run_hostile(stack, details=details)

    assert job.error == DECLINED
    assert response_of(steps)["refusal"] == {"category": None, "explanation": None}


@pytest.mark.parametrize("thinking", [-1, 1.5, "abc", [1], {"a": 1}, None], ids=str)
def test_a_hostile_thinking_count_is_left_out(stack: Stack, thinking: Any) -> None:
    job, steps, _ = run_hostile(stack, details=refusal(), thinking=thinking)

    assert job.error == WITH_CATEGORY
    assert "thinking_tokens" not in response_of(steps)["usage"]
    assert job.tokens_used == 12


@pytest.mark.parametrize("thinking", [True, "5"], ids=str)
def test_a_count_the_sdk_turned_into_an_int_is_stored_as_a_plain_count(
    stack: Stack, thinking: Any
) -> None:
    """The SDK's validation of a stream turns `true` into 1 and `"5"` into 5."""
    job, steps, _ = run_hostile(stack, details=refusal(), thinking=thinking)

    assert job.error == WITH_CATEGORY
    stored = response_of(steps)["usage"].get("thinking_tokens")
    assert stored is None or (type(stored) is int and stored >= 0)
    assert job.tokens_used == 12


# The thinking count


def test_a_reported_thinking_count_is_stored_and_not_counted_twice(stack: Stack) -> None:
    ran = stack.run_one(THINKING)

    usage = response_of(ran.steps)["usage"]
    assert ran.job.status is JobStatus.SUCCEEDED
    assert usage["thinking_tokens"] == 40
    assert usage["thinking_tokens"] <= usage["output_tokens"]
    # The provider's own count of the call: the thinking is inside `output_tokens`.
    billed = ran.requests[0]["usage"]
    assert usage["output_tokens"] == billed["output_tokens"]
    assert ran.job.tokens_used == billed["input_tokens"] + billed["output_tokens"]


def test_a_step_for_an_ordinary_answer_has_exactly_the_keys_it_had(stack: Stack) -> None:
    ran = stack.run_one(HELLO)

    responses = [step.payload for step in ran.steps if step.kind is K.MODEL_RESPONSE]
    assert ran.job.status is JobStatus.SUCCEEDED
    assert len(responses) == 2
    for payload in responses:
        assert set(payload) == ORDINARY_KEYS
        assert set(payload["usage"]) == ORDINARY_USAGE
