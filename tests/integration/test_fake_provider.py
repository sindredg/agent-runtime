"""The fake provider as deployed in the stack, over real HTTP."""

import json
from importlib import resources
from typing import Any
from uuid import uuid4

import httpx2

HEADERS = {"x-api-key": "test", "anthropic-version": "2026-10-21"}


def read_stream(response: httpx2.Response) -> list[dict[str, Any]]:
    """The `data` of each server-sent event, checked against its `event` line."""
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
            assert data["type"] == name
            events.append(data)
    return events


def test_streamed_response_matches_the_scenario_and_is_in_the_request_log(
    fake_provider_url: str,
) -> None:
    scripted = json.loads(
        (resources.files("fake_provider") / "scenarios" / "hello.json").read_text()
    )["turns"][0]["content"]
    user_id = f"test-{uuid4()}"
    body = {
        "model": "hello",
        "max_tokens": 1024,
        "stream": True,
        "metadata": {"user_id": user_id},
        "messages": [{"role": "user", "content": "Read hello.txt."}],
    }

    with httpx2.Client(base_url=fake_provider_url, timeout=10) as client:
        with client.stream("POST", "/v1/messages", json=body, headers=HEADERS) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = read_stream(response)
        log = client.get("/_fake/requests", params={"user_id": user_id}).json()

    # Rebuild the content from the stream alone.
    content: list[dict[str, Any]] = []
    partial: dict[int, str] = {}
    for event in events:
        if event["type"] == "content_block_start":
            content.append(dict(event["content_block"]))
            partial[event["index"]] = ""
        elif event["type"] == "content_block_delta":
            delta = event["delta"]
            if delta["type"] == "text_delta":
                content[event["index"]]["text"] += delta["text"]
            else:
                partial[event["index"]] += delta["partial_json"]
        elif (
            event["type"] == "content_block_stop" and content[event["index"]]["type"] == "tool_use"
        ):
            content[event["index"]]["input"] = json.loads(partial[event["index"]])

    assert [e["type"] for e in events][:1] == ["message_start"]
    assert [e["type"] for e in events][-2:] == ["message_delta", "message_stop"]
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    received = [{k: v for k, v in block.items() if k != "id"} for block in content]
    assert received == scripted

    assert len(log) == 1
    assert log[0]["body"] == body
    assert log[0]["status"] == 200
    assert (log[0]["scenario"], log[0]["turn"]) == ("hello", 0)
