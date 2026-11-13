"""The fake provider and the Claude API answer the same requests the same way.

Run by hand, never in CI: it calls the Claude API (four small requests) and
needs a key.

    make up
    ANTHROPIC_API_KEY=... CONTRACT_MODEL=<model id> uv run pytest tests/contract

The same two-turn exchange (a tool call, then the answer) runs against both
providers through the runtime's own client. Then the raw JSON of one
response from each is compared: everything the fake returns must also be in
what the real API returns.
"""

import asyncio
import os
from typing import Any

import httpx2
import pytest

from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider

FAKE_URL = os.environ.get("FAKE_PROVIDER_URL", "http://127.0.0.1:8001")
CLAUDE_URL = "https://api.anthropic.com"

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


def target(name: str) -> tuple[str, str, str]:
    """(base URL, API key, model) of the fake provider or the Claude API."""
    if name == "fake":
        return FAKE_URL, "contract-test", "hello"
    return CLAUDE_URL, setting("ANTHROPIC_API_KEY"), setting("CONTRACT_MODEL")


def exchange(base_url: str, api_key: str, model: str) -> tuple[ModelResponse, ModelResponse]:
    async def run() -> tuple[ModelResponse, ModelResponse]:
        provider = AnthropicProvider(api_key=api_key, base_url=base_url)

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
    first, second = exchange(*target(name))

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


def raw_message(base_url: str, api_key: str, model: str) -> dict[str, Any]:
    response = httpx2.post(
        f"{base_url}/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2026-10-21"},
        json={"model": model, "max_tokens": 2048, "tools": [TOOL], "messages": [PROMPT]},
        timeout=120,
    )
    response.raise_for_status()
    message: dict[str, Any] = response.json()
    return message


def test_everything_the_fake_returns_is_also_in_the_real_response() -> None:
    fake = raw_message(*target("fake"))
    real = raw_message(*target("claude"))

    assert set(fake) <= set(real)
    assert set(fake["usage"]) <= set(real["usage"])
    for field in ("type", "role", "stop_reason", "stop_sequence"):
        assert fake[field] == real[field]

    def keys_by_type(message: dict[str, Any]) -> dict[str, set[str]]:
        return {block["type"]: set(block) for block in message["content"]}

    real_blocks = keys_by_type(real)
    for block_type, keys in keys_by_type(fake).items():
        assert block_type in real_blocks
        assert keys <= real_blocks[block_type]


def test_both_refuse_an_unknown_model_with_the_same_error_shape() -> None:
    def refusal(base_url: str, api_key: str) -> tuple[int, dict[str, Any]]:
        response = httpx2.post(
            f"{base_url}/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2026-10-21"},
            json={"model": "no-such-model-contract-test", "max_tokens": 16, "messages": [PROMPT]},
            timeout=60,
        )
        return response.status_code, response.json()

    fake_status, fake = refusal(FAKE_URL, "contract-test")
    real_status, real = refusal(CLAUDE_URL, setting("ANTHROPIC_API_KEY"))
    assert fake_status == real_status == 404
    assert fake["type"] == real["type"] == "error"
    assert fake["error"]["type"] == real["error"]["type"] == "not_found_error"
    assert set(fake) <= set(real)
