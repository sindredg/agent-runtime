"""The fake provider in process: messages, event streams, errors, request log."""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from fake_provider.app import create_app
from fake_provider.messages import build_message, stream_events
from fake_provider.scenario import Scenario, parse_scenario

TWO_TURNS = parse_scenario(
    json.dumps(
        {
            "turns": [
                {
                    "content": [
                        {"type": "text", "text": "Reading the file now, one moment please."},
                        {"type": "tool_use", "name": "read_file", "input": {"path": "a.txt"}},
                    ]
                },
                {"content": [{"type": "text", "text": "Done."}]},
            ]
        }
    )
)
HEADERS = {"x-api-key": "test", "anthropic-version": "2026-10-21"}
USER = {"role": "user", "content": "go"}


def client(scenarios: dict[str, Scenario] | None = None) -> TestClient:
    return TestClient(create_app({"two": TWO_TURNS} if scenarios is None else scenarios))


def request(**changes: Any) -> dict[str, Any]:
    return {"model": "two", "max_tokens": 100, "messages": [USER], **changes}


def reassemble(events: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Rebuild the message from its events, the way a streaming client does."""
    message: dict[str, Any] = {}
    partial_json: dict[int, str] = {}
    for _, data in events:
        kind = data["type"]
        if kind == "message_start":
            message = json.loads(json.dumps(data["message"]))
        elif kind == "content_block_start":
            assert data["index"] == len(message["content"])
            message["content"].append(dict(data["content_block"]))
            partial_json[data["index"]] = ""
        elif kind == "content_block_delta":
            block, delta = message["content"][data["index"]], data["delta"]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            else:
                partial_json[data["index"]] += delta["partial_json"]
        elif kind == "content_block_stop":
            block = message["content"][data["index"]]
            if block["type"] == "tool_use":
                block["input"] = json.loads(partial_json[data["index"]])
        elif kind == "message_delta":
            message["stop_reason"] = data["delta"]["stop_reason"]
            message["usage"]["output_tokens"] = data["usage"]["output_tokens"]
    return message


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for record in text.strip().split("\n\n"):
        name, data = record.split("\n")
        assert name.startswith("event: ")
        assert data.startswith("data: ")
        events.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return events


# Messages and event streams


def test_first_turn_is_the_scripted_tool_call() -> None:
    message = client().post("/v1/messages", json=request(), headers=HEADERS).json()
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    assert message["model"] == "two"
    assert message["stop_reason"] == "tool_use"
    assert message["content"] == [
        {"type": "text", "text": "Reading the file now, one moment please."},
        {
            "type": "tool_use",
            "id": "toolu_fake_two_00_01",
            "name": "read_file",
            "input": {"path": "a.txt"},
        },
    ]
    assert message["usage"]["input_tokens"] >= 1
    assert message["usage"]["output_tokens"] >= 1


def second_request() -> dict[str, Any]:
    first = client().post("/v1/messages", json=request(), headers=HEADERS).json()
    result = {"type": "tool_result", "tool_use_id": "toolu_fake_two_00_01", "content": "x"}
    return request(
        messages=[
            USER,
            {"role": "assistant", "content": first["content"]},
            {"role": "user", "content": [result]},
        ]
    )


def test_turn_follows_the_number_of_assistant_messages() -> None:
    message = client().post("/v1/messages", json=second_request(), headers=HEADERS).json()
    assert message["stop_reason"] == "end_turn"
    assert message["content"] == [{"type": "text", "text": "Done."}]
    assert message["id"] == "msg_fake_two_01"


def test_the_same_request_gets_the_same_answer() -> None:
    api = client()
    first = api.post("/v1/messages", json=request(), headers=HEADERS).json()
    again = api.post("/v1/messages", json=request(), headers=HEADERS).json()
    assert first == again


def test_stream_has_the_api_event_order_and_rebuilds_the_message() -> None:
    api = client()
    whole = api.post("/v1/messages", json=request(), headers=HEADERS).json()
    response = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert all(name == data["type"] for name, data in events)
    names = [name for name, _ in events if name not in ("content_block_delta", "ping")]
    assert names == [
        "message_start",
        "content_block_start",
        "content_block_stop",
        "content_block_start",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    # The text is long enough to need more than one delta.
    text_deltas = [d for _, d in events if d.get("delta", {}).get("type") == "text_delta"]
    assert len(text_deltas) > 1
    assert events[0][1]["message"]["content"] == []
    assert events[0][1]["message"]["stop_reason"] is None
    assert reassemble(events) == whole


json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(),
    lambda children: st.lists(children) | st.dictionaries(st.text(), children),
    max_leaves=10,
)
blocks = st.one_of(
    st.fixed_dictionaries({"type": st.just("text"), "text": st.text(min_size=1)}),
    st.fixed_dictionaries(
        {
            "type": st.just("tool_use"),
            "name": st.text(min_size=1),
            "input": st.dictionaries(st.text(), json_values, max_size=4),
        }
    ),
)


@given(content=st.lists(blocks, min_size=1, max_size=5))
def test_any_turn_survives_the_event_stream(content: list[dict[str, Any]]) -> None:
    """Whatever a scenario scripts, the pieces of the stream add up to the whole message."""
    scenario = Scenario.model_validate({"turns": [{"content": content}]})
    message = build_message("s", scenario, 0, model="s", input_tokens=7)
    assert reassemble(list(stream_events(message))) == message
    scripted = [{k: v for k, v in block.items() if k != "id"} for block in message["content"]]
    assert scripted == content


# Errors


def error_of(response: Any) -> tuple[int, str]:
    body = response.json()
    assert body["type"] == "error"
    assert body["request_id"].startswith("req_fake_")
    assert body["error"]["message"]
    return response.status_code, body["error"]["type"]


@pytest.mark.parametrize(
    "body",
    [
        {"max_tokens": 1, "messages": [USER]},
        {"model": "two", "messages": [USER]},
        request(max_tokens=0),
        request(max_tokens="many"),
        request(messages=[]),
        request(messages=[{"role": "assistant", "content": "hi"}]),
        request(messages=[{"role": "system", "content": "hi"}]),
        request(messages=[USER, {"role": "assistant", "content": "hi"}]),
        request(messages=[{"role": "user", "content": 5}]),
        [],
    ],
)
def test_invalid_request_is_400(body: Any) -> None:
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")


def test_body_that_is_not_json_is_400() -> None:
    response = client().post("/v1/messages", content=b"{", headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")


def test_missing_key_is_401_and_missing_version_is_400() -> None:
    api = client()
    no_key = api.post("/v1/messages", json=request(), headers={"anthropic-version": "x"})
    assert error_of(no_key) == (401, "authentication_error")
    no_version = api.post("/v1/messages", json=request(), headers={"x-api-key": "k"})
    assert error_of(no_version) == (400, "invalid_request_error")


def test_unknown_model_is_404() -> None:
    response = client().post("/v1/messages", json=request(model="nope"), headers=HEADERS)
    assert error_of(response) == (404, "not_found_error")


def test_a_turn_past_the_end_of_the_scenario_is_400() -> None:
    body = second_request()
    body["messages"] += [{"role": "assistant", "content": "Done."}, USER]
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")
    assert "2 turns" in response.json()["error"]["message"]


RESULT = {"type": "tool_result", "tool_use_id": "toolu_fake_two_00_01", "content": "x"}


@pytest.mark.parametrize(
    ("answer", "named"),
    [
        ("no result here", "toolu_fake_two_00_01"),
        ([{"type": "tool_result", "tool_use_id": "other", "content": "x"}], "other"),
        ([RESULT, RESULT], "more than one tool_result"),
        ([RESULT, {**RESULT, "tool_use_id": "extra"}], "extra"),
        ([{**RESULT, "tool_use_id": ["a", "list"]}], "non-empty string"),
        ([{**RESULT, "tool_use_id": {"a": "dict"}}], "non-empty string"),
        ([{"type": "tool_result", "content": "x"}], "non-empty string"),
    ],
    ids=["missing", "wrong-id", "twice", "one-too-many", "list-id", "dict-id", "no-id"],
)
def test_tool_results_that_do_not_pair_with_the_tool_calls_are_400(answer: Any, named: str) -> None:
    body = second_request()
    body["messages"][2] = {"role": "user", "content": answer}
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")
    assert named in response.json()["error"]["message"]


def test_a_tool_result_without_any_tool_call_is_400() -> None:
    body = request(messages=[{"role": "user", "content": [RESULT]}])
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")


def test_two_tool_calls_with_one_id_in_an_assistant_message_are_400() -> None:
    call = {"type": "tool_use", "id": "same", "name": "t", "input": {}}
    body = request(
        messages=[
            USER,
            {"role": "assistant", "content": [call, call]},
            {"role": "user", "content": [{**RESULT, "tool_use_id": "same"}]},
        ]
    )
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")
    assert "unique" in response.json()["error"]["message"]


def test_a_tool_call_followed_by_another_assistant_message_is_400() -> None:
    call = {"type": "tool_use", "id": "lost", "name": "t", "input": {}}
    body = request(
        messages=[
            USER,
            {"role": "assistant", "content": [call]},
            {"role": "assistant", "content": "I never got an answer."},
            USER,
        ]
    )
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")
    assert "lost" in response.json()["error"]["message"]


def test_json_nested_too_deep_is_400_not_500() -> None:
    response = client().post("/v1/messages", content=b"[" * 200_000, headers=HEADERS)
    assert error_of(response) == (400, "invalid_request_error")


def test_text_that_is_not_valid_unicode_never_breaks_an_answer() -> None:
    """A lone surrogate arrives as a JSON escape. It must not make any response fail."""
    api = client()
    lone = json.dumps(request(model="\ud800"))
    unknown = api.post("/v1/messages", content=lone, headers=HEADERS)
    assert error_of(unknown) == (404, "not_found_error")
    in_text = json.dumps(request(messages=[{"role": "user", "content": "x \udfff y"}]))
    assert api.post("/v1/messages", content=in_text, headers=HEADERS).status_code == 200
    # The log holds both requests and can still be read.
    log = api.get("/_fake/requests")
    assert log.status_code == 200
    assert len(log.json()) == 2


# Request log


def test_every_request_is_logged_with_its_body_and_outcome() -> None:
    api = client()
    api.post("/v1/messages", json=request(metadata={"user_id": "job-1"}), headers=HEADERS)
    api.post("/v1/messages", json=request(model="nope"), headers=HEADERS)
    api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    log = api.get("/_fake/requests").json()
    assert [entry["seq"] for entry in log] == [1, 2, 3]
    assert [entry["status"] for entry in log] == [200, 404, 200]
    assert log[0]["body"] == request(metadata={"user_id": "job-1"})
    assert log[0]["user_id"] == "job-1"
    assert (log[0]["scenario"], log[0]["turn"]) == ("two", 0)
    assert log[0]["response_id"] == "msg_fake_two_00"
    assert log[0]["anthropic_version"] == "2026-10-21"
    assert "x-api-key" not in json.dumps(log)


def test_request_log_filters_by_user_id_and_can_be_cleared() -> None:
    api = client()
    for user in ("job-1", "job-2", "job-1"):
        api.post("/v1/messages", json=request(metadata={"user_id": user}), headers=HEADERS)
    assert len(api.get("/_fake/requests", params={"user_id": "job-1"}).json()) == 2
    assert api.get("/_fake/requests", params={"user_id": "job-3"}).json() == []
    assert api.delete("/_fake/requests").status_code == 204
    assert api.get("/_fake/requests").json() == []
