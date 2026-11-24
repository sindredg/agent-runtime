"""The fake provider's summaries: the answer to `tool_choice` `none`, markers, bad answers."""

import asyncio
import json
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from fake_provider.app import create_app
from fake_provider.scenario import Scenario, parse_scenario
from fake_provider.summary import place
from runtime.provider.base import ModelRequest, ModelResponse
from runtime.provider.messages import AnthropicProvider

LOCAL = "http://localhost"
HEADERS = {"x-api-key": "test", "anthropic-version": "2026-10-21"}
NONE = {"type": "none"}
TEXT = "What was done so far."


def read(path: str) -> dict[str, Any]:
    return {
        "content": [{"type": "tool_use", "name": "read_file", "input": {"path": path}}],
    }


def scenario(**summary: Any) -> Scenario:
    turns = [
        read("a.txt"),
        read("b.txt"),
        read("c.txt"),
        {"content": [{"type": "text", "text": "Done."}]},
    ]
    body: dict[str, Any] = {"turns": turns}
    if summary:
        body["summary"] = summary
    return parse_scenario(json.dumps(body))


def client(**summary: Any) -> TestClient:
    return TestClient(create_app({"long": scenario(**summary)}), base_url=LOCAL)


def post(http: TestClient, messages: list[dict[str, Any]], **extra: Any) -> Any:
    body = {"model": "long", "max_tokens": 1000, "messages": messages, **extra}
    return http.post("/v1/messages", json=body, headers=HEADERS)


def log(http: TestClient) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = http.get("/_fake/requests").json()
    return found


def answered(message: dict[str, Any], tool_use_id: str) -> list[dict[str, Any]]:
    """The assistant message and the user message that answers its one call."""
    return [
        {"role": "assistant", "content": message["content"]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
        },
    ]


def first_window(http: TestClient) -> list[dict[str, Any]]:
    """Two turns and their results: what a summary request is sent with."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for _ in range(2):
        message = post(http, messages).json()
        messages += answered(message, message["content"][0]["id"])
    return messages


def test_a_summary_request_without_a_summary_section_is_refused() -> None:
    http = client()
    response = post(http, [{"role": "user", "content": "go"}], tool_choice=NONE)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "tool_choice none" in response.json()["error"]["message"]
    assert log(http)[-1]["tool_choice"] == NONE


def test_the_log_records_the_tool_choice_and_none_without_one() -> None:
    http = client(text=TEXT)
    post(http, [{"role": "user", "content": "go"}])
    assert log(http)[-1]["tool_choice"] is None


def test_a_summary_carries_its_number_and_the_turn_reached() -> None:
    http = client(text=TEXT)
    messages = first_window(http)

    summary = post(http, messages, tool_choice=NONE).json()

    assert summary["stop_reason"] == "end_turn"
    assert summary["content"] == [{"type": "text", "text": f"{TEXT}\n\n[summary 1] [turn 2]"}]
    assert {key: log(http)[-1][key] for key in ("summary", "turn")} == {"summary": 1, "turn": 2}


def test_the_next_window_goes_on_at_the_turn_its_marker_names() -> None:
    http = client(text=TEXT)
    summary = post(http, first_window(http), tool_choice=NONE).json()
    opening = {"role": "user", "content": [{"type": "text", "text": summary["content"][0]["text"]}]}

    third = post(http, [opening]).json()

    assert third["content"][0]["input"] == {"path": "c.txt"}
    # Ids come from the job-wide turn, so they do not repeat those of window 1.
    assert third["content"][0]["id"] == "toolu_fake_long_02_00"
    assert third["id"] == "msg_fake_long_02"
    second = post(http, [opening, *answered(third, third["content"][0]["id"])], tool_choice=NONE)
    assert second.json()["content"][0]["text"].endswith("[summary 2] [turn 3]")


def test_a_summary_request_sent_again_gets_the_same_answer() -> None:
    http = client(text=TEXT)
    messages = first_window(http)
    assert post(http, messages, tool_choice=NONE).json() == (
        post(http, messages, tool_choice=NONE).json()
    )


def test_markers_are_read_from_the_first_user_message_only() -> None:
    assert place([{"role": "user", "content": "go"}]) == (0, 0)
    assert place(
        [{"role": "user", "content": [{"type": "text", "text": "x [summary 3] [turn 12]"}]}]
    ) == (3, 12)
    later = [{"role": "user", "content": "go"}, {"role": "user", "content": "[turn 9]"}]
    assert place(later) == (0, 0)


@pytest.mark.parametrize(
    ("fault", "stop_reason", "check"),
    [
        ("tool_use", "tool_use", lambda c: c[-1]["type"] == "tool_use"),
        ("refusal", "refusal", lambda c: c[0]["type"] == "text"),
        ("empty", "end_turn", lambda c: c == [{"type": "text", "text": ""}]),
        ("whitespace", "end_turn", lambda c: c[0]["text"].strip() == ""),
        ("max_tokens", "max_tokens", lambda c: c[0]["text"].startswith(TEXT)),
        ("stop_sequence", "stop_sequence", lambda c: c[0]["text"].startswith(TEXT)),
    ],
)
def test_a_scripted_bad_answer_is_given_to_its_summary_number_only(
    fault: str, stop_reason: str, check: Any
) -> None:
    http = client(text=TEXT, bad={"2": fault})
    messages = first_window(http)
    good = post(http, messages, tool_choice=NONE).json()
    opening = {"role": "user", "content": [{"type": "text", "text": good["content"][0]["text"]}]}
    third = post(http, [opening]).json()

    bad = post(http, [opening, *answered(third, third["content"][0]["id"])], tool_choice=NONE)

    assert good["stop_reason"] == "end_turn"
    assert bad.json()["stop_reason"] == stop_reason
    assert check(bad.json()["content"])
    if fault == "refusal":
        assert bad.json()["stop_details"]["category"] == "cyber"


def test_the_real_client_reads_a_stop_sequence_and_an_empty_text_from_the_stream() -> None:
    app = create_app({"long": scenario(text=TEXT, bad={"1": "stop_sequence"})})
    http = TestClient(app, base_url=LOCAL)
    messages = first_window(http)

    async def run() -> ModelResponse:
        transport = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
        provider = AnthropicProvider(api_key="k", base_url=LOCAL, http_client=transport)
        try:
            return await provider.complete(
                ModelRequest(model="long", max_tokens=1000, messages=messages, tool_choice=NONE)
            )
        finally:
            await provider.aclose()

    response = asyncio.run(run())
    assert response.stop_reason == "stop_sequence"
    assert response.content[0]["text"].startswith(TEXT)


@pytest.mark.parametrize(
    "summary",
    [
        {"text": ""},
        {"text": "x", "bad": {"0": "empty"}},
        {"text": "x", "bad": {"1": "silence"}},
        {"text": "x", "colour": "red"},
    ],
)
def test_a_summary_section_that_cannot_be_used_is_refused(summary: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scenario(**summary)
