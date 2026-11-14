"""G7, the budget: a job that loops never passes its token budget, by the provider's own count.

The fake provider reports input tokens as a quarter of the request's JSON and
logs the usage of each answer. The test adds those up for the job.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import Job, claim_job, create_job, get_job
from runtime.store.steps import StepKind, read_steps
from runtime.tools import default_tools
from runtime.worker.agent import run_job

from .conftest import Stack

BUDGET = 20_000
SPEC: dict[str, Any] = {
    "prompt": "Read notes.txt until you are done.",
    "model": "loop",
    "files": {"notes.txt": "a line of notes to read again and again\n" * 50},
}


def test_a_job_that_loops_stops_before_its_budget_by_the_providers_count(stack: Stack) -> None:
    async def body(engine: AsyncEngine, provider: AnthropicProvider) -> tuple[Job, list[Any]]:
        async with engine.begin() as conn:
            await create_job(
                conn,
                spec=SPEC,
                token_budget=BUDGET,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
            claimed = await claim_job(conn, worker_id="w1")
        assert claimed is not None
        await run_job(engine, provider, default_tools(), claimed, stack.settings())
        async with engine.begin() as conn:
            job = await get_job(conn, claimed.id)
            assert job is not None
            return job, await read_steps(conn, job.id)

    job, steps = stack.run(body)
    answered = [entry for entry in stack.requests_for(job) if entry.get("status") == 200]
    spent = sum(e["usage"]["input_tokens"] + e["usage"]["output_tokens"] for e in answered)
    requests = [step.payload for step in steps if step.kind is StepKind.MODEL_REQUEST]
    print(f"\n{len(answered)} calls, {spent} of {BUDGET} tokens by the provider's count")

    # The budget stopped it, after several rounds and well before the scenario's end.
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith("the token budget does not cover the next model call")
    assert 3 <= len(answered) < 30
    # By the provider's own count, and by the runtime's.
    assert spent <= BUDGET
    assert job.tokens_used == spent
    # Each call: the provider's input stayed within the bound, and the
    # count, the bound and max_tokens together fit the budget.
    assert len(requests) == len(answered)
    for request, entry in zip(requests, answered, strict=True):
        assert entry["usage"]["input_tokens"] <= request["input_bound"]
        assert request["tokens_used"] + request["input_bound"] + request["max_tokens"] <= BUDGET


class CutOff:
    """A provider whose one answer is cut off at max_tokens."""

    async def complete(self, request: ModelRequest) -> ModelResponse:
        text = [{"type": "text", "text": "x" * 40}]
        return ModelResponse("msg_1", "m", text, "max_tokens", 10, request.max_tokens)

    async def aclose(self) -> None:
        return None


def test_a_response_cut_off_at_a_max_tokens_the_budget_lowered_says_so(stack: Stack) -> None:
    async def body(engine: AsyncEngine, _: AnthropicProvider) -> Job:
        async with engine.begin() as conn:
            await create_job(
                conn,
                spec={"prompt": "go"},
                # Room for the first call's bound and about a hundred tokens more.
                token_budget=1_500,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
            claimed = await claim_job(conn, worker_id="w1")
        assert claimed is not None
        await run_job(engine, CutOff(), default_tools(), claimed, stack.settings())
        async with engine.begin() as conn:
            job = await get_job(conn, claimed.id)
            assert job is not None
            return job

    job = stack.run(body)

    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith("the model's response was cut off at max_tokens, which the token")
    assert job.tokens_used <= 1_500
