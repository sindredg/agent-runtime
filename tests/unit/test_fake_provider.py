"""The fake provider in process: messages, event streams, errors, request log."""

import asyncio
import hashlib
import json
from typing import Any

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from hypothesis import given
from hypothesis import strategies as st

from fake_provider.app import create_app
from fake_provider.faults import Fault, stream_parts
from fake_provider.messages import build_message, estimate_tokens, stream_events
from fake_provider.scenario import Scenario, parse_scenario
from runtime.provider.base import ModelRequest, ModelResponse, Refusal
from runtime.provider.messages import AnthropicProvider

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
# The fake answers only the names the stack and the tests use.
LOCAL = "http://localhost"
HEADERS = {"x-api-key": "test", "anthropic-version": "2026-10-21"}
USER = {"role": "user", "content": "go"}


def client(scenarios: dict[str, Scenario] | None = None) -> TestClient:
    return TestClient(
        create_app({"two": TWO_TURNS} if scenarios is None else scenarios), base_url=LOCAL
    )


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
    message = build_message("s", scenario, 0, model="s", input_tokens=7, max_tokens=100_000)
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


# Busy window: the rate limit and overload of the Claude API


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def busy_client() -> tuple[TestClient, Clock]:
    clock = Clock()
    return TestClient(
        create_app({"two": TWO_TURNS, "other": TWO_TURNS}, clock=clock), base_url=LOCAL
    ), clock


def test_a_busy_window_answers_429_with_retry_after_until_it_ends() -> None:
    api, clock = busy_client()
    set_busy = api.put("/_fake/busy", json={"seconds": 60, "retry_after": 5})
    assert set_busy.status_code == 204

    refused = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "5"
    assert refused.headers["request-id"] == refused.json()["request_id"]
    assert refused.json()["type"] == "error"
    assert refused.json()["error"]["type"] == "rate_limit_error"

    clock.now += 59.9
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 429
    clock.now += 0.1
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 200

    log = api.get("/_fake/requests").json()
    assert [entry["status"] for entry in log] == [429, 429, 200]
    assert log[0]["error"] == "rate_limit_error"
    assert log[0]["retry_after"] == 5


def test_an_overload_window_answers_529_and_may_leave_out_retry_after() -> None:
    api, _ = busy_client()
    api.put("/_fake/busy", json={"seconds": 1, "status": 529})
    refused = api.post("/v1/messages", json=request(), headers=HEADERS)
    assert refused.status_code == 529
    assert refused.json()["error"]["type"] == "overloaded_error"
    assert "retry-after" not in refused.headers


def test_a_busy_window_for_one_model_leaves_the_others_alone() -> None:
    api, _ = busy_client()
    api.put("/_fake/busy", json={"seconds": 60, "model": "other"})
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 200
    assert api.post("/v1/messages", json=request(model="other"), headers=HEADERS).status_code == 429


def test_a_busy_window_can_be_ended_and_replaced() -> None:
    api, clock = busy_client()
    api.put("/_fake/busy", json={"seconds": 60})
    assert api.delete("/_fake/busy").status_code == 204
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 200
    api.put("/_fake/busy", json={"seconds": 60, "status": 529})
    api.put("/_fake/busy", json={"seconds": 10})
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 429
    clock.now += 10
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"seconds": 0},
        {"seconds": 3601},
        {"seconds": "inf"},
        {"seconds": 5, "status": 500},
        {"seconds": 5, "retry_after": -1},
        {"seconds": 5, "colour": "red"},
    ],
)
def test_a_busy_window_that_makes_no_sense_is_refused(body: dict[str, Any]) -> None:
    api, _ = busy_client()
    assert api.put("/_fake/busy", json=body).status_code == 422
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 200


# Delay


def delayed(seconds: float) -> Scenario:
    return parse_scenario(
        json.dumps(
            {"turns": [{"delay_seconds": seconds, "content": [{"type": "text", "text": "late"}]}]}
        )
    )


@pytest.mark.parametrize("stream", [False, True])
def test_a_turn_with_a_delay_answers_that_much_later(stream: bool) -> None:
    import time

    api = client({"slow": delayed(0.3), "two": TWO_TURNS})
    started = time.monotonic()
    fast = api.post("/v1/messages", json=request(stream=stream), headers=HEADERS)
    fast_took = time.monotonic() - started
    started = time.monotonic()
    slow = api.post("/v1/messages", json=request(model="slow", stream=stream), headers=HEADERS)
    slow_took = time.monotonic() - started
    assert (fast.status_code, slow.status_code) == (200, 200)
    assert slow_took >= 0.3
    assert fast_took < 0.3


def test_a_delayed_stream_starts_at_once_and_pauses_before_its_content() -> None:
    """The wait falls inside the stream, so a client that is stopped during it is mid-stream."""
    from fake_provider.app import stream_parts

    message = build_message("slow", delayed(0.3), 0, model="slow", input_tokens=1, max_tokens=100)
    parts = list(stream_parts(message, delay=0.3))
    waits = [index for index, part in enumerate(parts) if part == 0.3]
    assert len(waits) == 1
    before = b"".join(part for part in parts[: waits[0]] if isinstance(part, bytes))
    after = b"".join(part for part in parts[waits[0] :] if isinstance(part, bytes))
    assert b"message_start" in before
    assert b"content_block_start" not in before
    assert b"content_block_start" in after
    assert b"message_stop" in after


# max_tokens


LONG = parse_scenario(
    json.dumps(
        {
            "turns": [
                {
                    "content": [
                        {"type": "text", "text": "x" * 4000},
                        {"type": "tool_use", "name": "read_file", "input": {"path": "a"}},
                    ]
                }
            ]
        }
    )
)


@pytest.mark.parametrize("max_tokens", [1, 9, 10, 50, 999])
def test_an_answer_never_has_more_output_tokens_than_max_tokens(max_tokens: int) -> None:
    """Like the API: a turn that needs more is cut off with stop_reason max_tokens."""
    body = {"model": "long", "max_tokens": max_tokens, "messages": [USER]}

    message = client({"long": LONG}).post("/v1/messages", json=body, headers=HEADERS).json()

    assert message["stop_reason"] == "max_tokens"
    assert message["usage"]["output_tokens"] == max_tokens
    # No tool call survives the cut, and the text left is no longer than the count.
    assert all(block["type"] == "text" for block in message["content"])
    assert estimate_tokens(message["content"]) <= max(max_tokens, 1)


def test_an_answer_that_fits_is_not_cut() -> None:
    body = {"model": "long", "max_tokens": 5000, "messages": [USER]}

    message = client({"long": LONG}).post("/v1/messages", json=body, headers=HEADERS).json()

    assert message["stop_reason"] == "tool_use"
    assert [block["type"] for block in message["content"]] == ["text", "tool_use"]


def test_the_request_log_has_the_usage_of_each_answer() -> None:
    app = client()
    message = app.post("/v1/messages", json=request(), headers=HEADERS).json()

    entry = app.get("/_fake/requests").json()[-1]

    assert entry["usage"] == {
        "input_tokens": message["usage"]["input_tokens"],
        "output_tokens": message["usage"]["output_tokens"],
    }


def fault_client() -> TestClient:
    # A broken stream raises inside the app on purpose, to drop the connection.
    return TestClient(
        create_app({"two": TWO_TURNS, "other": TWO_TURNS}),
        base_url=LOCAL,
        raise_server_exceptions=False,
    )


def test_a_fault_answers_exactly_the_next_requests_for_its_model() -> None:
    api = fault_client()
    set_fault = api.put(
        "/_fake/fault", json={"model": "two", "count": 2, "kind": "status", "status": 500}
    )
    assert set_fault.status_code == 204

    other = api.post("/v1/messages", json=request(model="other"), headers=HEADERS)
    assert other.status_code == 200
    statuses = [
        api.post("/v1/messages", json=request(), headers=HEADERS).status_code for _ in range(3)
    ]
    assert statuses == [500, 500, 200]

    log = api.get("/_fake/requests").json()
    assert [entry.get("fault") for entry in log] == [None, "status", "status", None]
    assert log[1]["status"] == 500
    assert log[1]["error"] == "api_error"


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "invalid_request_error"),
        (408, "timeout_error"),
        (409, "conflict_error"),
        (500, "api_error"),
        (502, "api_error"),
        (503, "api_error"),
        (504, "timeout_error"),
    ],
)
def test_a_status_fault_answers_with_the_apis_error_type(status: int, kind: str) -> None:
    api = fault_client()
    api.put(
        "/_fake/fault",
        json={"model": "two", "count": 1, "kind": "status", "status": status, "retry_after": 7},
    )
    answered = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert answered.status_code == status
    assert answered.json()["error"]["type"] == kind
    assert answered.headers["retry-after"] == "7"
    assert answered.headers["request-id"] == answered.json()["request_id"]
    [entry] = api.get("/_fake/requests").json()
    assert (entry["status"], entry["error"], entry["retry_after"]) == (status, kind, 7)


@pytest.mark.parametrize("kind", ["api_error", "timeout_error"])
def test_a_stream_error_comes_after_some_output(kind: str) -> None:
    api = fault_client()
    api.put(
        "/_fake/fault", json={"model": "two", "count": 1, "kind": "stream_error", "error": kind}
    )
    answered = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert answered.status_code == 200
    assert "event: content_block_delta" in answered.text
    assert "event: error" in answered.text
    assert f'"type": "{kind}"' in answered.text
    assert "event: message_delta" not in answered.text
    [entry] = api.get("/_fake/requests").json()
    assert (entry["fault"], entry["status"], entry["error"]) == ("stream_error", 200, kind)


def test_a_stream_without_its_stop_reason_ends_before_message_delta() -> None:
    api = fault_client()
    api.put("/_fake/fault", json={"model": "two", "count": 1, "kind": "no_stop_reason"})
    answered = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert answered.status_code == 200
    assert "event: content_block_stop" in answered.text
    assert "event: message_delta" not in answered.text
    assert "event: message_stop" not in answered.text


def test_a_broken_stream_stays_in_the_log_and_sends_no_end() -> None:
    fault = Fault(model="two", count=1, kind="broken_stream")
    parts = b"".join(stream_parts(fault, "two", 1))
    assert b"event: content_block_delta" in parts
    assert b"event: message_delta" not in parts

    api = fault_client()
    api.put("/_fake/fault", json={"model": "two", "count": 1, "kind": "broken_stream"})
    api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    [entry] = api.get("/_fake/requests").json()
    assert (entry["fault"], entry["status"]) == ("broken_stream", 200)


def test_a_200_that_is_not_a_message_stream() -> None:
    api = fault_client()
    api.put("/_fake/fault", json={"model": "two", "count": 1, "kind": "not_a_stream"})
    answered = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert answered.status_code == 200
    assert answered.headers["content-type"].startswith("text/html")
    assert "event:" not in answered.text


def test_a_held_request_is_answered_504_after_the_hold() -> None:
    api = fault_client()
    api.put("/_fake/fault", json={"model": "two", "count": 1, "kind": "hold", "seconds": 0.01})
    answered = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert answered.status_code == 504
    [entry] = api.get("/_fake/requests").json()
    assert (entry["fault"], entry["status"], entry["error"]) == ("hold", 504, "timeout_error")


@pytest.mark.parametrize(("kind", "status"), [("spend_cap", 429), ("stream_spend_cap", 200)])
def test_a_spend_cap_carries_its_error_code(kind: str, status: int) -> None:
    api = fault_client()
    api.put("/_fake/fault", json={"model": "two", "count": 1, "kind": kind})
    answered = api.post("/v1/messages", json=request(stream=True), headers=HEADERS)
    assert answered.status_code == status
    assert "enforced_spend_limit_reached" in answered.text


def test_a_fault_can_be_cleared_and_replaced() -> None:
    api = fault_client()
    five = {"model": "two", "count": 5, "kind": "status", "status": 500}
    api.put("/_fake/fault", json=five)
    assert api.delete("/_fake/fault").status_code == 204
    assert api.post("/v1/messages", json=request(), headers=HEADERS).status_code == 200

    api.put("/_fake/fault", json=five)
    api.put("/_fake/fault", json={**five, "count": 1, "status": 503})
    first = api.post("/v1/messages", json=request(), headers=HEADERS)
    second = api.post("/v1/messages", json=request(), headers=HEADERS)
    assert (first.status_code, second.status_code) == (503, 200)


@pytest.mark.parametrize(
    "body",
    [
        {"count": 1, "kind": "status", "status": 500},
        {"model": "two", "kind": "status", "status": 500},
        {"model": "two", "count": 0, "kind": "status", "status": 500},
        {"model": "two", "count": 1, "kind": "status"},
        {"model": "two", "count": 1, "kind": "status", "status": 418},
        {"model": "two", "count": 1, "kind": "status", "status": 529},
        {"model": "two", "count": 1, "kind": "stream_error"},
        {"model": "two", "count": 1, "kind": "stream_error", "error": "invalid_request_error"},
        {"model": "two", "count": 1, "kind": "hold"},
        {"model": "two", "count": 1, "kind": "hold", "seconds": 61},
        {"model": "two", "count": 1, "kind": "broken_stream", "status": 500},
        {"model": "two", "count": 1, "kind": "not_a_stream", "retry_after": 5},
        {"model": "two", "count": 1, "kind": "unknown"},
        {"model": "two", "count": 1, "kind": "not_a_stream", "extra": 1},
    ],
)
def test_a_fault_that_makes_no_sense_is_refused(body: dict[str, Any]) -> None:
    assert fault_client().put("/_fake/fault", json=body).status_code == 422


def test_the_log_keeps_a_hash_of_the_bytes_received() -> None:
    api = fault_client()
    raw = json.dumps(request()).encode()
    for content in (raw, raw, raw + b" "):
        api.post("/v1/messages", content=content, headers=HEADERS)
    first, second, third = (entry["body_sha256"] for entry in api.get("/_fake/requests").json())
    assert first == second == hashlib.sha256(raw).hexdigest()
    assert third != first


# A refusal's details and the thinking count

TEXT_BLOCK = {"type": "text", "text": "I cannot help with that."}
REFUSING = parse_scenario(
    json.dumps(
        {
            "turns": [
                {
                    "content": [TEXT_BLOCK],
                    "stop_reason": "refusal",
                    "stop_details": {"category": "cyber", "explanation": "the reason"},
                    "thinking_tokens": 7,
                }
            ]
        }
    )
)
BARE_REFUSAL = parse_scenario(
    json.dumps({"turns": [{"content": [TEXT_BLOCK], "stop_reason": "refusal"}]})
)
DETAILS = {"type": "refusal", "category": "cyber", "explanation": "the reason"}


def refusing(**changes: Any) -> dict[str, Any]:
    return {"model": "refusing", "max_tokens": 1000, "messages": [USER], **changes}


def test_a_json_answer_carries_the_details_and_the_thinking_count() -> None:
    body = refusing()

    message = client({"refusing": REFUSING}).post("/v1/messages", json=body, headers=HEADERS).json()

    assert message["stop_reason"] == "refusal"
    assert message["stop_details"] == DETAILS
    assert message["usage"]["output_tokens_details"] == {"thinking_tokens": 7}
    # The count is part of the output tokens, as in the API.
    assert message["usage"]["output_tokens"] == estimate_tokens(message["content"]) + 7


def test_the_stream_carries_them_on_message_delta_only() -> None:
    body = refusing(stream=True)
    answer = client({"refusing": REFUSING}).post("/v1/messages", json=body, headers=HEADERS)

    events = dict(parse_sse(answer.text))

    assert "stop_details" not in events["message_start"]["message"]
    assert "output_tokens_details" not in events["message_start"]["message"]["usage"]
    assert events["message_delta"]["delta"]["stop_details"] == DETAILS
    assert events["message_delta"]["usage"]["output_tokens_details"] == {"thinking_tokens": 7}


def test_a_turn_that_sets_neither_sends_no_new_field() -> None:
    plain = client().post("/v1/messages", json=request(), headers=HEADERS).json()
    streamed = client().post("/v1/messages", json=request(stream=True), headers=HEADERS)
    events = dict(parse_sse(streamed.text))

    assert set(plain) == {
        "id",
        "type",
        "role",
        "model",
        "content",
        "stop_reason",
        "stop_sequence",
        "usage",
    }
    assert set(plain["usage"]) == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    assert set(events["message_delta"]["delta"]) == {"stop_reason", "stop_sequence"}
    assert set(events["message_delta"]["usage"]) == {"output_tokens"}


def test_a_refusal_without_details_sends_no_details() -> None:
    body = refusing()
    message = client({"refusing": BARE_REFUSAL}).post("/v1/messages", json=body, headers=HEADERS)
    assert message.json()["stop_reason"] == "refusal"
    assert "stop_details" not in message.json()
    assert "output_tokens_details" not in message.json()["usage"]


def test_a_refusal_cut_off_at_max_tokens_is_no_refusal() -> None:
    body = refusing(max_tokens=3)

    message = client({"refusing": REFUSING}).post("/v1/messages", json=body, headers=HEADERS).json()

    assert message["stop_reason"] == "max_tokens"
    assert "stop_details" not in message
    assert message["usage"]["output_tokens"] == 3
    assert message["usage"]["output_tokens_details"]["thinking_tokens"] <= 3


def fake_transport() -> httpx2.ASGITransport:
    return httpx2.ASGITransport(app=create_app({"refusing": REFUSING, "bare": BARE_REFUSAL}))


def test_the_real_sdk_reads_them_from_the_json_form() -> None:
    async def run() -> anthropic.types.Message:
        async with httpx2.AsyncClient(transport=fake_transport()) as http:
            sdk = anthropic.AsyncAnthropic(
                api_key="k", base_url=LOCAL, http_client=http, max_retries=0
            )
            return await sdk.messages.create(
                model="refusing",
                max_tokens=1000,
                messages=[{"role": "user", "content": "go"}],
            )

    message = asyncio.run(run())

    assert message.stop_details is not None
    assert (message.stop_details.category, message.stop_details.explanation) == (
        "cyber",
        "the reason",
    )
    assert message.usage.output_tokens_details is not None
    assert message.usage.output_tokens_details.thinking_tokens == 7


@pytest.mark.parametrize(
    ("model", "refusal", "thinking"),
    [("refusing", Refusal("cyber", "the reason"), 7), ("bare", Refusal(None, None), None)],
)
def test_the_runtime_reads_them_from_the_stream_through_the_real_sdk(
    model: str, refusal: Refusal, thinking: int | None
) -> None:
    async def run() -> ModelResponse:
        http = httpx2.AsyncClient(transport=fake_transport())
        provider = AnthropicProvider(api_key="k", base_url=LOCAL, http_client=http)
        try:
            return await provider.complete(
                ModelRequest(
                    model=model, max_tokens=1000, messages=[{"role": "user", "content": "go"}]
                )
            )
        finally:
            await provider.aclose()

    response = asyncio.run(run())

    assert response.stop_reason == "refusal"
    assert response.refusal == refusal
    assert response.thinking_tokens == thinking


# The context window


def windowed(window: int) -> TestClient:
    return client({"two": TWO_TURNS.model_copy(update={"context_window": window})})


def test_a_request_over_the_context_window_is_refused_as_the_api_refuses_it() -> None:
    size = estimate_tokens({"system": None, "tools": None, "messages": [USER]})
    api = windowed(size - 1)

    response = api.post("/v1/messages", json=request(), headers=HEADERS)

    assert error_of(response) == (400, "invalid_request_error")
    message = f"prompt is too long: {size} tokens > {size - 1} maximum"
    assert response.json()["error"]["message"] == message
    (entry,) = api.get("/_fake/requests").json()
    assert (entry["status"], entry["error"], entry["input_estimate"]) == (400, message, size)


def test_a_request_at_the_context_window_is_answered_and_logs_its_estimate() -> None:
    size = estimate_tokens({"system": None, "tools": None, "messages": [USER]})
    api = windowed(size)

    response = api.post("/v1/messages", json=request(), headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["usage"]["input_tokens"] == size
    (entry,) = api.get("/_fake/requests").json()
    assert entry["input_estimate"] == entry["usage"]["input_tokens"] == size


def test_the_window_is_checked_before_the_turn_and_the_summary() -> None:
    """A request past the script, or a summary request, over the window gets the window error."""
    past = second_request()
    past["messages"] += [{"role": "assistant", "content": "Done."}, USER]
    summary = request(tool_choice={"type": "none"})
    api = windowed(1)

    for body in (past, summary):
        response = api.post("/v1/messages", json=body, headers=HEADERS)
        assert error_of(response) == (400, "invalid_request_error")
        assert response.json()["error"]["message"].startswith("prompt is too long: ")


def test_a_scenario_without_a_window_answers_any_size() -> None:
    body = request(system="x" * 100_000)
    response = client().post("/v1/messages", json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["usage"]["input_tokens"] > 25_000
