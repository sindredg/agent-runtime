"""The crossing count of a long job, on a made-up step log and a stub counter.

The count says how much text a job's stored steps hold, in the model's own
tokens. Here the counter is a stub that answers the UTF-8 bytes of a
request's text plus 7, so every expected number is worked out by hand. No
request leaves the process.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SUITE = Path(__file__).resolve().parents[1] / "long_job"
# In every part of the log that is not counted.
MARKER = "NOT-COUNTED"
# What the stub adds to the bytes of a request's text.
STUB_EXTRA = 7
BASELINE = 1 + STUB_EXTRA


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"long_job_{name}_counted", SUITE / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def counting() -> ModuleType:
    return load("counting")


@pytest.fixture(scope="module")
def rules() -> ModuleType:
    return load("rules")


class Stub:
    """A counter that keeps every request and answers the bytes of its text plus 7."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: dict[str, Any]) -> int:
        self.requests.append(request)
        (message,) = request["messages"]
        return len(message["content"].encode()) + STUB_EXTRA

    @property
    def lengths(self) -> list[int]:
        return [len(request["messages"][0]["content"].encode()) for request in self.requests]


def step(seq: int, kind: str, **payload: Any) -> dict[str, Any]:
    return {"seq": seq, "kind": kind, "payload": payload, "lease_epoch": 1}


def result(seq: int, content: Any, is_error: bool = False) -> dict[str, Any]:
    return step(seq, "tool_result", tool_use_id=f"toolu_{seq}", content=content, is_error=is_error)


RESPONSE_TEXT = "Reading on." + '{"b":1,"path":"é.txt"}'
SUMMARY_TEXT = "The task and what was read." + "More of it."


def fixture_log(limit: int) -> list[dict[str, Any]]:
    """A log of every step kind. Its counted text makes three chunks at `limit` bytes.

    Chunk 1: the response's text and tool input, a result of 200,000 bytes
    and an error result of 300,000. Chunk 2: one result of exactly `limit`
    bytes. Chunk 3: the summary's two text blocks and a result of six bytes.
    """
    return [
        step(1, "model_request", model=MARKER, tools=[MARKER], messages=1),
        step(
            2,
            "model_response",
            content=[
                {"type": "thinking", "thinking": MARKER, "signature": MARKER},
                {"type": "redacted_thinking", "data": MARKER},
                {"type": "text", "text": "Reading on."},
                {
                    "type": "tool_use",
                    "id": MARKER,
                    "name": MARKER,
                    "input": {"path": "é.txt", "b": 1},
                },
            ],
            stop_reason="tool_use",
            usage={"input_tokens": 10, "output_tokens": 5},
        ),
        step(3, "tool_intent", tool_use_id="toolu_4", name="read_file", input={"path": MARKER}),
        result(4, "x" * 200_000),
        result(5, "y" * 300_000, is_error=True),
        step(6, "approval_requested", operation_id=MARKER, reason=MARKER),
        result(7, "z" * limit),
        step(8, "model_request", summary=True, tools=[MARKER]),
        step(
            9,
            "summary",
            content=[
                {"type": "text", "text": "The task and what was read."},
                {"type": "thinking", "thinking": MARKER, "signature": MARKER},
                {"type": "text", "text": "More of it."},
            ],
            message={"role": "user", "content": [{"type": "text", "text": MARKER}]},
            stop_reason="end_turn",
        ),
        step(
            10,
            "model_response",
            content=[{"type": "text", "text": MARKER}],
            content_dropped=True,
            problem="it cannot be stored",
            stop_reason="end_turn",
        ),
        step(11, "error", error=MARKER),
        step(12, "status_changed", to="waiting", cause=MARKER),
        result(13, "æøå"),
        step(14, "approval_given", question=6, note=MARKER),
        step(15, "notification_sent", url=MARKER),
    ]


def count(counting: ModuleType, rules: ModuleType, steps: list[dict[str, Any]], stub: Stub) -> Any:
    """The count with the line's own model and chunk size."""
    return counting.crossing(steps, stub, model=rules.MODEL, chunk_bytes=rules.CHUNK_BYTES)


def test_the_total_of_the_fixture_log_is_the_one_worked_out_by_hand(
    counting: ModuleType, rules: ModuleType
) -> None:
    limit = rules.CHUNK_BYTES
    stub = Stub()
    counted = count(counting, rules, fixture_log(limit), stub)
    first = len(RESPONSE_TEXT.encode()) + 200_000 + 300_000
    third = len(SUMMARY_TEXT.encode()) + 6

    assert limit == 524_288
    assert (len(RESPONSE_TEXT.encode()), len(SUMMARY_TEXT.encode())) == (34, 38)
    # The request with one character first, then a request for each chunk.
    assert stub.lengths == [1, first, limit, third] == [1, 500_034, 524_288, 44]
    # Each chunk's count less the count of the one-character request.
    by_hand = (first + 7 - 8) + (limit + 7 - 8) + (third + 7 - 8)
    assert by_hand == 1_024_363
    assert counted == counting.Count(total=by_hand, chunks=3, partial=False)


def test_each_request_names_the_model_and_is_one_user_message_of_text(
    counting: ModuleType, rules: ModuleType
) -> None:
    stub = Stub()
    count(counting, rules, fixture_log(rules.CHUNK_BYTES), stub)
    texts = [request["messages"][0]["content"] for request in stub.requests]

    assert len(stub.requests) == 4
    for request, text in zip(stub.requests, texts, strict=True):
        assert request == {
            "model": "claude-haiku-5-5",
            "messages": [{"role": "user", "content": text}],
        }
        assert isinstance(text, str)
    assert texts[0] == "a"
    # Pieces and steps are joined with nothing between them.
    assert texts[1] == RESPONSE_TEXT + "x" * 200_000 + "y" * 300_000
    assert texts[3] == SUMMARY_TEXT + "æøå"


def test_nothing_that_is_not_counted_is_sent(counting: ModuleType, rules: ModuleType) -> None:
    steps = fixture_log(rules.CHUNK_BYTES)
    stub = Stub()
    count(counting, rules, steps, stub)

    # The marker is in eleven steps of the log, and in no request.
    assert sum(MARKER in json.dumps(step) for step in steps) == 11
    assert not [request for request in stub.requests if MARKER in json.dumps(request)]


def test_a_tool_input_is_counted_as_sorted_compact_json_with_its_own_characters(
    counting: ModuleType, rules: ModuleType
) -> None:
    given = {"title": "Ærlig: «x»", "body": {"b": [1, 2], "a": None}}
    response = step(1, "model_response", content=[{"type": "tool_use", "input": given}])
    stub = Stub()
    count(counting, rules, [response], stub)

    assert stub.requests[1]["messages"][0]["content"] == (
        '{"body":{"a":null,"b":[1,2]},"title":"Ærlig: «x»"}'
    )


def test_steps_are_counted_in_seq_order_whatever_order_they_come_in(
    counting: ModuleType, rules: ModuleType
) -> None:
    stub = Stub()
    count(counting, rules, [result(3, "c"), result(1, "a"), result(2, "b")], stub)

    assert [request["messages"][0]["content"] for request in stub.requests] == ["a", "abc"]


def test_a_chunk_is_never_split_inside_a_step(counting: ModuleType, rules: ModuleType) -> None:
    """Two steps of a byte over half the limit do not share a chunk, and neither is cut."""
    half = rules.CHUNK_BYTES // 2 + 1
    stub = Stub()
    counted = count(counting, rules, [result(1, "a" * half), result(2, "b" * half)], stub)

    assert stub.lengths == [1, half, half]
    assert counted == counting.Count(total=2 * (half - 1), chunks=2, partial=False)


def test_the_limit_is_in_utf8_bytes_not_characters(counting: ModuleType, rules: ModuleType) -> None:
    fits = "é" * (rules.CHUNK_BYTES // 2)
    stub = Stub()
    counted = count(counting, rules, [result(1, fits)], stub)

    assert len(fits) * 2 == len(fits.encode()) == rules.CHUNK_BYTES
    assert stub.lengths == [1, rules.CHUNK_BYTES]
    assert counted.partial is False
    over = count(counting, rules, [result(1, fits + "é")], Stub())
    assert over == counting.Count(total=0, chunks=0, partial=True)


def test_a_step_over_the_limit_makes_the_count_partial_and_is_left_out(
    counting: ModuleType, rules: ModuleType
) -> None:
    limit = rules.CHUNK_BYTES
    steps = [*fixture_log(limit), result(16, "W" * (limit + 1)), result(17, "tail")]
    stub = Stub()
    counted = count(counting, rules, steps, stub)

    # The step of one byte too many is in no request; the step after it is.
    assert stub.lengths == [1, 500_034, 524_288, 44 + 4]
    assert counted == counting.Count(total=1_024_363 + 4, chunks=3, partial=True)
    assert not [request for request in stub.requests if "W" in request["messages"][0]["content"]]


@pytest.mark.parametrize(
    "unreadable",
    [
        result(2, ["not", "text"]),
        result(2, None),
        step(2, "model_response", content="not a list of blocks"),
        step(2, "model_response", content=[{"type": "text", "text": 5}]),
        step(2, "model_response", content=[{"type": "tool_use", "input": "not an object"}]),
        step(2, "summary", content=[["not a block"]]),
    ],
)
def test_a_counted_step_whose_text_cannot_be_read_makes_the_count_partial(
    counting: ModuleType, rules: ModuleType, unreadable: dict[str, Any]
) -> None:
    stub = Stub()
    counted = count(counting, rules, [result(1, "before"), unreadable, result(3, "after")], stub)

    assert stub.lengths == [1, len("beforeafter")]
    assert counted == counting.Count(total=10, chunks=1, partial=True)


def test_a_log_with_no_counted_text_counts_nothing_and_asks_nothing(
    counting: ModuleType, rules: ModuleType
) -> None:
    quiet = [step(1, "model_request", tools=[]), step(2, "tool_intent", name="read_file")]
    stub = Stub()

    assert count(counting, rules, quiet, stub) == counting.Count(total=0, chunks=0, partial=False)
    assert count(counting, rules, [], stub) == counting.Count(total=0, chunks=0, partial=False)
    assert stub.requests == []


def test_the_one_character_request_is_made_once(counting: ModuleType, rules: ModuleType) -> None:
    steps = [result(n, "r" * 300_000) for n in range(1, 8)]
    stub = Stub()
    counted = count(counting, rules, steps, stub)

    assert stub.lengths == [1, *[300_000] * 7]
    assert counted == counting.Count(total=7 * (300_000 - 1), chunks=7, partial=False)


# The counter over a client


def test_the_counter_asks_the_clients_count_and_returns_its_input_tokens(
    counting: ModuleType,
) -> None:
    """A stand-in for the client: any object with `messages.count_tokens`."""
    asked: list[dict[str, Any]] = []

    def count_tokens(**request: Any) -> SimpleNamespace:
        asked.append(request)
        return SimpleNamespace(input_tokens=41, output_tokens=MARKER)

    client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count_tokens))
    request = {"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "a"}]}

    assert counting.counter(client)(request) == 41
    assert asked == [request]


def test_a_count_through_the_counter_is_the_same_as_through_the_stub(
    counting: ModuleType, rules: ModuleType
) -> None:
    def count_tokens(*, model: str, messages: list[dict[str, str]]) -> SimpleNamespace:
        assert model == rules.MODEL
        return SimpleNamespace(input_tokens=len(messages[0]["content"].encode()) + STUB_EXTRA)

    client = SimpleNamespace(messages=SimpleNamespace(count_tokens=count_tokens))
    steps = fixture_log(rules.CHUNK_BYTES)
    through_client = counting.crossing(
        steps, counting.counter(client), model=rules.MODEL, chunk_bytes=rules.CHUNK_BYTES
    )

    assert through_client == count(counting, rules, steps, Stub())
    assert BASELINE == 8
