"""The provider client's handling of failures, against a stubbed HTTP transport.

The successful path is tested against the fake provider over real HTTP in
tests/integration.
"""

import asyncio
import json
from collections.abc import Callable

import httpx2
import pytest

from fake_provider.messages import encode_sse, stream_events
from runtime.provider.base import ModelRequest, ModelResponse, ProviderError
from runtime.provider.messages import CLAUDE_API_URL, AnthropicProvider

REQUEST = ModelRequest(
    model="m", max_tokens=100, messages=[{"role": "user", "content": "hi"}], user_id="job-1"
)
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "hello there, this is a longer text"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 9},
}
STREAM = [encode_sse(event, data) for event, data in stream_events(MESSAGE)]


def complete(handler: Callable[[httpx2.Request], httpx2.Response]) -> ModelResponse:
    async def run() -> ModelResponse:
        http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        provider = AnthropicProvider(
            api_key="k", base_url="http://provider", http_client=http_client
        )
        try:
            return await provider.complete(REQUEST)
        finally:
            await provider.aclose()

    return asyncio.run(run())


def sse(chunks: list[bytes]) -> httpx2.Response:
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(chunks)
    )


def error(status: int, kind: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": kind, "message": "nope"}}
    )


def test_a_complete_stream_becomes_the_response() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return sse(STREAM)

    response = complete(handler)
    assert response == ModelResponse(
        id="msg_1",
        model="m",
        content=[{"type": "text", "text": "hello there, this is a longer text"}],
        stop_reason="end_turn",
        input_tokens=3,
        output_tokens=9,
    )
    sent = json.loads(seen[0].content)
    assert sent["stream"] is True
    assert sent["metadata"] == {"user_id": "job-1"}
    assert "system" not in sent
    assert "tools" not in sent


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (400, "invalid_request_error", False),
        (401, "authentication_error", False),
        (404, "not_found_error", False),
        (413, "request_too_large", False),
        (429, "rate_limit_error", True),
        (500, "api_error", True),
        (529, "overloaded_error", True),
    ],
)
def test_an_error_status_is_classified(status: int, kind: str, retryable: bool) -> None:
    with pytest.raises(ProviderError) as raised:
        complete(lambda request: error(status, kind))
    assert raised.value.status == status
    assert raised.value.retryable is retryable


def test_the_client_does_not_retry_by_itself() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return error(429, "rate_limit_error")

    with pytest.raises(ProviderError):
        complete(handler)
    assert calls == 1


@pytest.mark.parametrize("keep", [1, 3, len(STREAM) - 2])
def test_a_stream_that_ends_early_is_an_error_not_a_short_answer(keep: int) -> None:
    """The stream stops before message_delta: nothing of the fragment is returned."""
    with pytest.raises(ProviderError) as raised:
        complete(lambda request: sse(STREAM[:keep]))
    assert raised.value.retryable is True
    assert raised.value.status is None


def test_a_connection_failure_is_retryable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(ProviderError) as raised:
        complete(handler)
    assert raised.value.retryable is True
    assert raised.value.status is None


def stream_error(kind: str) -> bytes:
    body = {"type": "error", "error": {"type": kind, "message": "nope"}}
    return encode_sse("error", body)


@pytest.mark.parametrize(
    ("kind", "retryable"),
    [
        ("overloaded_error", True),
        ("api_error", True),
        ("rate_limit_error", True),
        ("invalid_request_error", False),
    ],
)
def test_an_error_event_inside_a_started_stream_is_classified_by_its_type(
    kind: str, retryable: bool
) -> None:
    """The HTTP status was 200. The error arrives as an event after some output."""
    with pytest.raises(ProviderError) as raised:
        complete(lambda request: sse([*STREAM[:3], stream_error(kind)]))
    assert raised.value.retryable is retryable
    assert raised.value.status is None
    assert kind in str(raised.value)


@pytest.mark.parametrize(
    "response",
    [
        sse([]),
        sse([encode_sse("ping", {"type": "ping"})]),
        sse([b"event: message_start\ndata: {not json\n\n"]),
        httpx2.Response(200, json={"hello": "not a stream"}),
        httpx2.Response(200, text="<html>a proxy's page</html>"),
        httpx2.Response(204),
    ],
    ids=["empty", "ping-only", "bad-json-line", "json-body", "html-body", "no-content"],
)
def test_a_200_that_is_not_a_message_stream_is_a_provider_error(response: httpx2.Response) -> None:
    with pytest.raises(ProviderError) as raised:
        complete(lambda request: response)
    assert raised.value.retryable is True
    assert raised.value.status is None


def test_the_base_url_never_comes_from_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK reads ANTHROPIC_BASE_URL itself when given none. The client always gives one."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://somewhere-else:1")
    default = AnthropicProvider(api_key="k", base_url=None)
    assert str(default._client.base_url).rstrip("/") == CLAUDE_API_URL
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    assert str(AnthropicProvider(api_key="k")._client.base_url).rstrip("/") == CLAUDE_API_URL
    chosen = AnthropicProvider(api_key="k", base_url="http://fake:8000")
    assert str(chosen._client.base_url).rstrip("/") == "http://fake:8000"
