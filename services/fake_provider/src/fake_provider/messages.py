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


def cut_to_max_tokens(content: list[dict[str, Any]], max_tokens: int) -> list[dict[str, Any]]:
    """What is left of a message cut off at `max_tokens`: the start of its text, no tool calls.

    The API stops writing at `max_tokens` and may stop in the middle of a
    block. The fake keeps it simple and stays within the count.
    """
    text = "".join(block["text"] for block in content if block["type"] == "text")
    # Four characters per token, less the JSON around the block.
    room = max(0, max_tokens * 4 - len(json.dumps([{"type": "text", "text": ""}])))
    return [{"type": "text", "text": text[:room]}] if room else []


def build_message(
    name: str,
    scenario: Scenario,
    turn_index: int,
    *,
    model: str,
    input_tokens: int,
    max_tokens: int,
) -> dict[str, Any]:
    """The assistant message for one turn. Ids are fixed, so a resent request gets the same ones.

    Like the API, it never has more than `max_tokens` output tokens: a turn
    that would need more is cut off, with the stop reason `max_tokens`.
    """
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
    stop_reason: str = turn.resolved_stop_reason
    output_tokens = estimate_tokens(content) + (turn.thinking_tokens or 0)
    if output_tokens > max_tokens:
        content, stop_reason, output_tokens = (
            cut_to_max_tokens(content, max_tokens),
            "max_tokens",
            max_tokens,
        )
    message: dict[str, Any] = {
        "id": f"msg_fake_{name}_{turn_index:02d}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    # Only for a turn that sets them, so an ordinary answer is unchanged.
    if turn.thinking_tokens is not None:
        message["usage"]["output_tokens_details"] = {
            "thinking_tokens": min(turn.thinking_tokens, output_tokens)
        }
    if stop_reason == "refusal" and turn.stop_details is not None:
        message["stop_details"] = {
            "type": "refusal",
            "category": turn.stop_details.category,
            "explanation": turn.stop_details.explanation,
        }
    return message


def _chunks(text: str) -> Iterator[str]:
    for start in range(0, len(text), CHUNK_SIZE):
        yield text[start : start + CHUNK_SIZE]


def stream_events(message: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """The server-sent events that deliver `message`, as (event name, data) pairs.

    The order is the API's: message_start, then per content block a start,
    its deltas and a stop, then message_delta with the stop reason and the
    final output token count, then message_stop. A refusal's `stop_details`
    and the thinking count come on message_delta, as they do from the API.
    """
    usage = message["usage"]
    opening = {
        **{key: value for key, value in message.items() if key != "stop_details"},
        "content": [],
        "stop_reason": None,
        "usage": {
            **{key: value for key, value in usage.items() if key != "output_tokens_details"},
            "output_tokens": 1,
        },
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
    ending: dict[str, Any] = {
        "stop_reason": message["stop_reason"],
        "stop_sequence": message.get("stop_sequence"),
    }
    if "stop_details" in message:
        ending["stop_details"] = message["stop_details"]
    final_usage: dict[str, Any] = {"output_tokens": usage["output_tokens"]}
    if "output_tokens_details" in usage:
        final_usage["output_tokens_details"] = usage["output_tokens_details"]
    yield "message_delta", {"type": "message_delta", "delta": ending, "usage": final_usage}
    yield "message_stop", {"type": "message_stop"}


def encode_sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
