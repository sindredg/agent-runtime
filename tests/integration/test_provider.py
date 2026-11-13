"""The provider client against the fake provider, over real HTTP."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx2
import pytest

from runtime.provider.base import ModelRequest, ModelResponse, ProviderError
from runtime.provider.messages import AnthropicProvider

TOOL = {
    "name": "read_file",
    "description": "Read a text file from the workspace.",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
}
PROMPT = {"role": "user", "content": "Read hello.txt and say what it holds."}


def with_provider[T](url: str, body: Callable[[AnthropicProvider], Awaitable[T]]) -> T:
    async def run() -> T:
        provider = AnthropicProvider(api_key="test", base_url=url)
        try:
            return await body(provider)
        finally:
            await provider.aclose()

    return asyncio.run(run())


def request_log(url: str, user_id: str) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = httpx2.get(
        f"{url}/_fake/requests", params={"user_id": user_id}, timeout=10
    ).json()
    return log


def test_two_turns_with_a_tool_call(fake_provider_url: str) -> None:
    user_id = f"test-{uuid4()}"

    def request(messages: list[dict[str, Any]]) -> ModelRequest:
        return ModelRequest(
            model="hello",
            max_tokens=1024,
            messages=messages,
            system="Be brief.",
            tools=(TOOL,),
            user_id=user_id,
        )

    async def body(provider: AnthropicProvider) -> tuple[ModelResponse, ModelResponse]:
        first = await provider.complete(request([PROMPT]))
        tool_use = first.content[-1]
        result = {"type": "tool_result", "tool_use_id": tool_use["id"], "content": "Hello!"}
        second = await provider.complete(
            request(
                [
                    PROMPT,
                    # The content goes back exactly as it was received.
                    {"role": "assistant", "content": first.content},
                    {"role": "user", "content": [result]},
                ]
            )
        )
        return first, second

    first, second = with_provider(fake_provider_url, body)

    assert first.stop_reason == "tool_use"
    assert [block["type"] for block in first.content] == ["text", "tool_use"]
    assert first.content[1]["name"] == "read_file"
    assert first.content[1]["input"] == {"path": "hello.txt"}
    assert first.model == "hello"
    assert first.input_tokens >= 1
    assert first.output_tokens >= 1
    assert second.stop_reason == "end_turn"
    assert [block["type"] for block in second.content] == ["text"]

    # The provider's own record of what it was asked.
    log = request_log(fake_provider_url, user_id)
    assert [(entry["status"], entry["turn"]) for entry in log] == [(200, 0), (200, 1)]
    sent = log[0]["body"]
    assert sent["stream"] is True
    assert sent["model"] == "hello"
    assert sent["max_tokens"] == 1024
    assert sent["system"] == "Be brief."
    assert sent["tools"] == [TOOL]
    assert sent["messages"] == [PROMPT]
    assert log[1]["body"]["messages"][1] == {"role": "assistant", "content": first.content}


def test_an_unknown_model_is_a_provider_error_that_is_not_retryable(fake_provider_url: str) -> None:
    request = ModelRequest(model="no-such-scenario", max_tokens=10, messages=[PROMPT])
    with pytest.raises(ProviderError) as raised:
        with_provider(fake_provider_url, lambda provider: provider.complete(request))
    assert raised.value.status == 404
    assert raised.value.retryable is False


def test_a_provider_that_is_not_there_is_retryable() -> None:
    request = ModelRequest(model="hello", max_tokens=10, messages=[PROMPT])
    with pytest.raises(ProviderError) as raised:
        # Port 9 (discard) is closed on the test machine.
        with_provider("http://127.0.0.1:9", lambda provider: provider.complete(request))
    assert raised.value.status is None
    assert raised.value.retryable is True
