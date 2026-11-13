"""The Anthropic Messages wire format: the response message and its event stream."""

import json
from collections.abc import Iterator
from typing import Any

from fake_provider.scenario import Scenario, TextBlock

# Characters per streamed delta. Small, so even a short text arrives in several pieces.
CHUNK_SIZE = 16


def estimate_tokens(value: Any) -> int:
    """A stable stand-in for a token count: one token per four characters of JSON."""
    return max(1, len(json.dumps(value)) // 4)


def build_message(
    name: str, scenario: Scenario, turn_index: int, *, model: str, input_tokens: int
) -> dict[str, Any]:
    """The assistant message for one turn. Ids are fixed, so a resent request gets the same ones."""
    turn = scenario.turns[turn_index]
    content: list[dict[str, Any]] = []
    for index, block in enumerate(turn.content):
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        else:
            content.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_fake_{name}_{turn_index:02d}_{index:02d}",
                    "name": block.name,
                    "input": block.input,
                }
            )
    return {
        "id": f"msg_fake_{name}_{turn_index:02d}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": turn.resolved_stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": estimate_tokens(content),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _chunks(text: str) -> Iterator[str]:
    for start in range(0, len(text), CHUNK_SIZE):
        yield text[start : start + CHUNK_SIZE]


def stream_events(message: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """The server-sent events that deliver `message`, as (event name, data) pairs.

    The order is the API's: message_start, then per content block a start,
    its deltas and a stop, then message_delta with the stop reason and the
    final output token count, then message_stop.
    """
    usage = message["usage"]
    opening = {
        **message,
        "content": [],
        "stop_reason": None,
        "usage": {**usage, "output_tokens": 1},
    }
    yield "message_start", {"type": "message_start", "message": opening}
    yield "ping", {"type": "ping"}
    for index, block in enumerate(message["content"]):
        start: dict[str, Any]
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": piece} for piece in _chunks(block["text"])]
        else:
            # A tool call's input arrives as pieces of its JSON text.
            start = {**block, "input": {}}
            deltas = [
                {"type": "input_json_delta", "partial_json": piece}
                for piece in _chunks(json.dumps(block["input"]))
            ]
        yield (
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": start},
        )
        for delta in deltas:
            yield (
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            )
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    yield (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
            "usage": {"output_tokens": usage["output_tokens"]},
        },
    )
    yield "message_stop", {"type": "message_stop"}


def encode_sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
