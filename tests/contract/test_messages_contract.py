"""The fake provider and the Claude API answer the same requests the same way.

Started by hand, never in CI: it calls the Claude API (eight small requests,
one of them refused on purpose) and needs a key. A recorded run starts from
the Manual runs workflow (docs/contract-test.md). On a local machine:

    make up
    ANTHROPIC_API_KEY=... CONTRACT_MODEL=<model id> uv run pytest tests/contract

The same two-turn exchange (a tool call, then the answer) runs against both
providers through the runtime's own client. Then the raw JSON of the same
exchange from each is compared, both ways: everything the fake returns must
also be in what the real API returns, and everything the API returns and the
fake does not must be declared in NOT_MODELLED. The raw responses and that
list are written to `.contract/<time>/`.
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from runtime.logs import json_safe, text_safe
from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider
from runtime.worker.budget import input_bound
from runtime.worker.summary import (
    TOOL_CHOICE_NONE,
    TOOL_CHOICE_ONE_CALL,
    summary_messages,
    summary_problem,
)

FAKE_URL = os.environ.get("FAKE_PROVIDER_URL", "http://127.0.0.1:8001")
CLAUDE_URL = "https://api.anthropic.com"
REPORTS = Path(__file__).resolve().parents[2] / ".contract"

# What the API may return and the fake does not, as `api_only` names it.
# Why each is not modelled: docs/contract-test.md, "What the fake does not model".
# Only entries an API run has shown are listed.
NOT_MODELLED = frozenset(
    {
        "block type thinking",
        "tool_use block field caller",
        "more than one tool_use block in a response",
        "message field container",
        "message field diagnostics",
        "message field stop_details",
        "usage field cache_creation",
        "usage field inference_geo",
        "usage field output_tokens_details",
        "usage field service_tier",
        "usage field speed",
    }
)

TOOL = {
    "name": "read_file",
    "description": "Read a text file from the workspace. Returns the file's text.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Path inside the workspace"}},
        "required": ["path"],
    },
}
PROMPT = {
    "role": "user",
    "content": (
        "Use the read_file tool to read hello.txt, then tell me in one sentence what it says."
    ),
}


def setting(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.fail(f"{name} is not set; see this file's docstring")
    return value


class Hidden(str):
    """A secret that prints as stars.

    pytest prints the arguments of the call that failed, and a recorded
    run's log is public. No function here takes the key as an argument; this
    covers the calls into other code that do.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "'***'"


def target(name: str) -> tuple[str, Hidden, str]:
    """(base URL, API key, model) of the fake provider or the Claude API."""
    if name == "fake":
        return FAKE_URL, Hidden("contract-test"), "hello"
    return CLAUDE_URL, Hidden(setting("ANTHROPIC_API_KEY")), setting("CONTRACT_MODEL")


def exchange(name: str) -> tuple[ModelResponse, ModelResponse]:
    async def run() -> tuple[ModelResponse, ModelResponse]:
        base_url, secret, model = target(name)
        provider = AnthropicProvider(api_key=secret, base_url=base_url)

        def request(messages: list[dict[str, Any]]) -> ModelRequest:
            return ModelRequest(
                model=model, max_tokens=2048, messages=messages, tools=(TOOL,), user_id="contract"
            )

        try:
            first = await provider.complete(request([PROMPT]))
            calls = [block for block in first.content if block["type"] == "tool_use"]
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": call["id"],
                    "content": "Hello from the file.",
                }
                for call in calls
            ]
            second = await provider.complete(
                request(
                    [
                        PROMPT,
                        {"role": "assistant", "content": first.content},
                        {"role": "user", "content": results},
                    ]
                )
            )
        finally:
            await provider.aclose()
        return first, second

    return asyncio.run(run())


@pytest.mark.parametrize("name", ["fake", "claude"])
def test_tool_call_then_answer(name: str) -> None:
    first, second = exchange(name)

    assert first.stop_reason == "tool_use"
    calls = [block for block in first.content if block["type"] == "tool_use"]
    assert [call["name"] for call in calls] == ["read_file"]
    assert calls[0]["input"] == {"path": "hello.txt"}
    assert calls[0]["id"]
    assert first.input_tokens > 0
    assert first.output_tokens > 0

    # The second request sent the first response's content back unchanged,
    # and the provider accepted it.
    assert second.stop_reason == "end_turn"
    assert any(block["type"] == "text" and block["text"] for block in second.content)

    # The budget's bound (ADR 0010) holds against the provider's own count.
    # It rests on a token never being shorter than a byte.
    results = [
        {"type": "tool_result", "tool_use_id": call["id"], "content": "Hello from the file."}
        for call in calls
    ]
    first_bound = input_bound(
        system=None, tools=[TOOL], messages=[PROMPT], counted=None, counted_messages=0
    )
    second_bound = input_bound(
        system=None,
        tools=[TOOL],
        messages=[
            PROMPT,
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": results},
        ],
        counted=first.input_tokens + first.output_tokens,
        counted_messages=2,
    )
    assert first.input_tokens <= first_bound
    assert second.input_tokens <= second_bound


def summary_exchange(name: str) -> ModelResponse:
    """A tool call, then the summary request the worker sends after its result (ADR 0033)."""

    async def run() -> ModelResponse:
        base_url, secret, model = target(name)
        if name == "fake":
            # The fake answers a summary request only in a scenario with a summary.
            model = "summary"
        provider = AnthropicProvider(api_key=secret, base_url=base_url)
        try:
            first = await provider.complete(
                ModelRequest(
                    model=model,
                    max_tokens=2048,
                    messages=[PROMPT],
                    tools=(TOOL,),
                    user_id="contract",
                )
            )
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": "Hello from the file.",
                }
                for block in first.content
                if block["type"] == "tool_use"
            ]
            conversation: list[dict[str, Any]] = [
                PROMPT,
                {"role": "assistant", "content": first.content},
                {"role": "user", "content": results},
            ]
            return await provider.complete(
                ModelRequest(
                    model=model,
                    max_tokens=2048,
                    messages=summary_messages(conversation),
                    tools=(TOOL,),
                    user_id="contract",
                    tool_choice=TOOL_CHOICE_NONE,
                )
            )
        finally:
            await provider.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("name", ["fake", "claude"])
def test_a_summary_request_keeps_the_tools_and_is_answered_with_text_only(name: str) -> None:
    """The shape of the summary request: `tool_choice` `none`, the tools kept, and the
    instruction as a text block after the last `tool_result`. The provider accepts it,
    and the answer is a usable summary."""
    summary = summary_exchange(name)

    assert summary_problem(summary.stop_reason, summary.content) is None
    assert all(block["type"] != "tool_use" for block in summary.content)


TWO_FILES = {
    "role": "user",
    "content": (
        "Use the read_file tool to read a.txt and b.txt, then tell me in one sentence what"
        " each says."
    ),
}


def one_call_answer(name: str) -> ModelResponse:
    """The answer to a request that asks for one tool call, as a job with a trigger sends it."""

    async def run() -> ModelResponse:
        base_url, secret, model = target(name)
        provider = AnthropicProvider(api_key=secret, base_url=base_url)
        try:
            return await provider.complete(
                ModelRequest(
                    model=model,
                    max_tokens=2048,
                    messages=[TWO_FILES],
                    tools=(TOOL,),
                    user_id="contract",
                    tool_choice=TOOL_CHOICE_ONE_CALL,
                )
            )
        finally:
            await provider.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("name", ["fake", "claude"])
def test_a_one_call_request_is_answered_with_one_tool_call_at_most(name: str) -> None:
    """`tool_choice` `auto` with parallel tool use off, and a prompt that names two files.
    The provider accepts it, and the answer calls one tool at most."""
    answer = one_call_answer(name)

    assert len([block for block in answer.content if block["type"] == "tool_use"]) <= 1


def raw_message(name: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    base_url, secret, model = target(name)
    response = httpx2.post(
        f"{base_url}/v1/messages",
        headers={"x-api-key": secret, "anthropic-version": "2026-10-21"},
        json={"model": model, "max_tokens": 2048, "tools": [TOOL], "messages": messages},
        timeout=120,
    )
    response.raise_for_status()
    message: dict[str, Any] = response.json()
    return message


def raw_exchange(name: str) -> list[dict[str, Any]]:
    """The raw JSON of both responses of the exchange: the tool call, then the answer."""
    first = raw_message(name, [PROMPT])
    results = [
        {"type": "tool_result", "tool_use_id": block["id"], "content": "Hello from the file."}
        for block in first["content"]
        if block["type"] == "tool_use"
    ]
    second = raw_message(
        name,
        [
            PROMPT,
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": results},
        ],
    )
    return [first, second]


def blocks_by_type(responses: list[dict[str, Any]]) -> dict[str, list[set[str]]]:
    """The field names of every content block of the exchange, grouped by block type."""
    blocks: dict[str, list[set[str]]] = {}
    for response in responses:
        for block in response["content"]:
            blocks.setdefault(block["type"], []).append(set(block))
    return blocks


def differences(fake: list[dict[str, Any]], real: list[dict[str, Any]]) -> list[str]:
    """What the fake returns that the real API does not. Empty when there is nothing.

    Both lists hold the responses of the same exchange, turn by turn. The
    message fields are compared turn by turn. Content blocks are compared by
    type over the whole exchange: a model may write text before it calls a
    tool or not, so the turn a block type shows up in is the model's choice
    and not the shape of the API.
    """
    found = []
    if len(fake) != len(real):
        return [f"the fake gave {len(fake)} responses, the API {len(real)}"]
    for turn, (ours, theirs) in enumerate(zip(fake, real, strict=True)):
        if extra := set(ours) - set(theirs):
            found.append(f"turn {turn}: message fields only the fake has: {sorted(extra)}")
        if extra := set(ours["usage"]) - set(theirs["usage"]):
            found.append(f"turn {turn}: usage fields only the fake has: {sorted(extra)}")
        found += [
            f"turn {turn}: {field} is {ours[field]!r} in the fake and {theirs[field]!r} in the API"
            for field in ("type", "role", "stop_reason", "stop_sequence")
            if ours[field] != theirs[field]
        ]

    real_blocks = blocks_by_type(real)
    for block_type, blocks in blocks_by_type(fake).items():
        if block_type not in real_blocks:
            found.append(f"the API sent no {block_type} block in the whole exchange")
            continue
        # Every field the fake puts on any block of the type has to be on
        # every block of that type the API sent.
        always = set.intersection(*real_blocks[block_type])
        if extra := set.union(*blocks) - always:
            found.append(f"{block_type} block fields only the fake has: {sorted(extra)}")
    return found


def api_only(fake: list[dict[str, Any]], real: list[dict[str, Any]]) -> list[str]:
    """What the API returned and the fake did not, over the whole exchange. Sorted.

    The other direction of `differences`. Compared over the whole exchange,
    not turn by turn: the question is what the worker can meet at all.
    """

    def names(responses: list[dict[str, Any]], part: str | None = None) -> set[str]:
        return {name for r in responses for name in (r[part] if part else r)}

    def most_calls(responses: list[dict[str, Any]]) -> int:
        return max(len([b for b in r["content"] if b["type"] == "tool_use"]) for r in responses)

    found = {f"message field {name}" for name in names(real) - names(fake)}
    found |= {f"usage field {name}" for name in names(real, "usage") - names(fake, "usage")}
    found |= {
        f"stop reason {reason}"
        for reason in {r["stop_reason"] for r in real} - {r["stop_reason"] for r in fake}
    }
    fake_blocks = blocks_by_type(fake)
    for block_type, blocks in blocks_by_type(real).items():
        if block_type not in fake_blocks:
            found.add(f"block type {block_type}")
            continue
        ours = {name for block in fake_blocks[block_type] for name in block}
        found |= {f"{block_type} block field {name}" for block in blocks for name in block - ours}
    if most_calls(real) > 1 >= most_calls(fake):
        found.add("more than one tool_use block in a response")
    return sorted(found)


@pytest.fixture(scope="module")
def exchanges() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The raw exchange with each provider, once, written to `.contract/<time>/`."""
    fake = raw_exchange("fake")
    real = raw_exchange("claude")
    report = REPORTS / datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    write_report(report, fake, real)
    return fake, real


def write_report(report: Path, fake: list[dict[str, Any]], real: list[dict[str, Any]]) -> None:
    """Both exchanges and what only the API sent, with workflow-command markers escaped."""
    report.mkdir(parents=True)
    (report / "fake.json").write_text(json_safe(json.dumps(fake, indent=2)) + "\n")
    (report / "api.json").write_text(json_safe(json.dumps(real, indent=2)) + "\n")
    (report / "api-only.txt").write_text(
        text_safe("".join(f"{line}\n" for line in api_only(fake, real)))
    )


def test_everything_the_fake_returns_is_also_in_the_real_responses(
    exchanges: tuple[list[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    fake, real = exchanges
    assert differences(fake, real) == []


def test_what_only_the_api_returns_is_declared(
    exchanges: tuple[list[dict[str, Any]], list[dict[str, Any]]],
) -> None:
    """A new entry is a decision: model it in the fake, or declare it and say why."""
    fake, real = exchanges
    assert [entry for entry in api_only(fake, real) if entry not in NOT_MODELLED] == []


def test_both_refuse_an_unknown_model_with_the_same_error_shape() -> None:
    def refusal(name: str) -> tuple[int, dict[str, Any]]:
        base_url, secret, _ = target(name)
        response = httpx2.post(
            f"{base_url}/v1/messages",
            headers={"x-api-key": secret, "anthropic-version": "2026-10-21"},
            json={"model": "no-such-model-contract-test", "max_tokens": 16, "messages": [PROMPT]},
            timeout=60,
        )
        return response.status_code, response.json()

    fake_status, fake = refusal("fake")
    real_status, real = refusal("claude")
    assert fake_status == real_status == 404
    assert fake["type"] == real["type"] == "error"
    assert fake["error"]["type"] == real["error"]["type"] == "not_found_error"
    assert set(fake) <= set(real)
