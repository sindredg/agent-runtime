"""The token budget's bound of a model call's input, and the max_tokens it leaves."""

import json
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from runtime.worker.budget import ALLOWANCE, input_bound, max_tokens_within

TOOLS = [{"name": "read_file", "description": "Read a file.", "input_schema": {"type": "object"}}]
PROMPT = {"role": "user", "content": "go"}
ANSWER = {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}
RESULT = {
    "role": "user",
    "content": [{"type": "tool_result", "tool_use_id": "t", "content": "é" * 10}],
}


def size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode())


def test_the_first_call_is_bounded_by_the_bytes_of_the_whole_request() -> None:
    bound = input_bound(
        system="be brief", tools=TOOLS, messages=[PROMPT], counted=None, counted_messages=0
    )

    assert bound == size({"system": "be brief", "tools": TOOLS, "messages": [PROMPT]}) + ALLOWANCE


def test_a_later_call_adds_the_bytes_since_the_last_response_to_its_count() -> None:
    messages: list[dict[str, Any]] = [PROMPT, ANSWER, RESULT]

    bound = input_bound(
        system=None, tools=TOOLS, messages=messages, counted=500, counted_messages=2
    )

    # Bytes, not characters: "é" is two bytes as UTF-8.
    assert bound == 500 + size([RESULT]) + ALLOWANCE


@given(text=st.text())
def test_the_bound_is_never_below_one_token_per_byte_of_the_request(text: str) -> None:
    """The assumption the bound rests on, applied to the first call."""
    message: dict[str, Any] = {"role": "user", "content": text}

    bound = input_bound(system=None, tools=[], messages=[message], counted=None, counted_messages=0)

    assert bound >= len(text.encode())


@pytest.mark.parametrize(
    ("budget", "used", "bound", "wanted", "expected"),
    [
        (10_000, 0, 2_000, 1_024, 1_024),
        (10_000, 7_000, 2_000, 1_024, 1_000),
        (10_000, 7_999, 2_000, 1_024, 1),
        (10_000, 8_000, 2_000, 1_024, None),
        (10_000, 9_500, 2_000, 1_024, None),
    ],
)
def test_max_tokens_is_what_is_left_after_the_bound(
    budget: int, used: int, bound: int, wanted: int, expected: int | None
) -> None:
    assert max_tokens_within(budget, used, bound, wanted) == expected
