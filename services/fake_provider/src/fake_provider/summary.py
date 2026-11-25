"""The fake's summaries, and the markers that carry its place across them.

Each summary ends with two markers: `[summary K]`, its number in the job,
and `[turn N]`, the job-wide turn reached. The worker opens the next window
with that text, so the next request's first user message holds them. The
fake reads them back and counts turns from N, so the script goes on after a
summary instead of starting again. Both come from the request alone: a
request sent again gets the same answer.
"""

import re
from typing import Any

from fake_provider.messages import cut_to_max_tokens, estimate_tokens
from fake_provider.scenario import Summary

# The line the fake writes at the end of each summary, on a line of its own.
# Only that exact line counts, so a prompt that mentions a turn moves nothing.
_MARKERS = re.compile(r"^\[summary (\d{1,6})\] \[turn (\d{1,6})\]$", re.MULTILINE)
# What a scripted refusal of a summary says.
REFUSAL_DETAILS = {
    "type": "refusal",
    "category": "cyber",
    "explanation": "The fake provider declined to write the summary.",
}


def _first_text(messages: list[dict[str, Any]]) -> str:
    """The text of the first user message."""
    content = messages[0].get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


def place(messages: list[dict[str, Any]]) -> tuple[int, int]:
    """(summaries so far, the turn the window starts at), from the first user message.

    (0, 0) without markers: the first window of a job.
    """
    found = _MARKERS.findall(_first_text(messages))
    if not found:
        return 0, 0
    summaries, turn = found[-1]
    return int(summaries), int(turn)


def summary_text(section: Summary, number: int, turn: int) -> str:
    """The text of summary `number`, written when the job has reached `turn`."""
    return f"{section.text}\n\n[summary {number}] [turn {turn}]"


def build_summary(
    name: str,
    section: Summary,
    number: int,
    turn: int,
    *,
    model: str,
    input_tokens: int,
    max_tokens: int,
) -> dict[str, Any]:
    """The answer to summary request `number`: the summary, or its scripted bad answer."""
    text = summary_text(section, number, turn)
    fault = section.bad.get(number)
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    stop_reason, stop_sequence = "end_turn", None
    if fault == "tool_use":
        call = section.bad_call
        content.append(
            {
                "type": "tool_use",
                "id": f"toolu_fake_{name}_summary_{number:02d}",
                "name": "read_file" if call is None else call.name,
                "input": {"path": "summary.txt"} if call is None else call.input,
            }
        )
        stop_reason = "tool_use"
    elif fault == "refusal":
        content, stop_reason = [{"type": "text", "text": "I will not summarise this."}], "refusal"
    elif fault == "empty":
        content = [{"type": "text", "text": ""}]
    elif fault == "whitespace":
        content = [{"type": "text", "text": " \n\t \n"}]
    elif fault == "max_tokens":
        stop_reason = "max_tokens"
    elif fault == "stop_sequence":
        stop_reason, stop_sequence = "stop_sequence", "END"
    output_tokens = estimate_tokens(content)
    if output_tokens > max_tokens:
        content, stop_reason, output_tokens = (
            cut_to_max_tokens(content, max_tokens),
            "max_tokens",
            max_tokens,
        )
    message: dict[str, Any] = {
        "id": f"msg_fake_{name}_summary_{number:02d}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": stop_sequence,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }
    if stop_reason == "refusal":
        message["stop_details"] = dict(REFUSAL_DETAILS)
    return message
