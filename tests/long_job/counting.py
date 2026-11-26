"""The crossing count: how much text a job's stored steps hold, in a model's tokens.

The text of the steps is put together in seq order, cut into chunks at step
boundaries, and each chunk is counted as one user message. What counts the
tokens is a callable the caller brings. Loading this module imports no
client and reads no key. Only `keyed_client` builds one, when it is called,
for a run on the Claude API.

Counted: the content of a `tool_result`, error results too; the text blocks
and the tool inputs of a `model_response`; the text blocks of a `summary`.
Not counted: every other step kind, a summary's opening message, thinking,
and a step whose content was dropped.
"""

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

type Request = dict[str, Any]
type Counter = Callable[[Request], int]

# The text of the request each chunk's count is compared with.
ONE_CHARACTER = "a"


@dataclass(frozen=True)
class Count:
    """The tokens of a log's counted text, in how many requests.

    `partial`: a step was left out, because its text is over a chunk or
    cannot be read. The total is then a lower bound.
    """

    total: int
    chunks: int
    partial: bool


def canonical(value: Any) -> str:
    """A tool input as JSON: keys sorted, no white space, characters as they are."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _blocks(content: Any, with_inputs: bool) -> str | None:
    """The text blocks of a message, and its tool inputs if asked, joined with nothing."""
    if not isinstance(content, list):
        return None
    pieces = []
    for block in content:
        if not isinstance(block, dict):
            return None
        if block.get("type") == "text":
            if not isinstance(block.get("text"), str):
                return None
            pieces.append(block["text"])
        elif block.get("type") == "tool_use" and with_inputs:
            if not isinstance(block.get("input"), dict):
                return None
            pieces.append(canonical(block["input"]))
    return "".join(pieces)


def step_text(step: dict[str, Any]) -> str | None:
    """The counted text of a step. Empty for a step that is not counted, None if unreadable."""
    kind, payload = step["kind"], step["payload"]
    if payload.get("content_dropped"):
        return ""
    if kind == "tool_result":
        content = payload.get("content")
        return content if isinstance(content, str) else None
    if kind == "model_response":
        return _blocks(payload.get("content"), with_inputs=True)
    if kind == "summary":
        return _blocks(payload.get("content"), with_inputs=False)
    return ""


def chunks(steps: Sequence[dict[str, Any]], chunk_bytes: int) -> tuple[list[str], bool]:
    """The counted text in seq order, in chunks of at most `chunk_bytes` UTF-8 bytes.

    A chunk ends at a step boundary and is filled as far as the next step
    allows. The second value says a step was left out: its text is over
    `chunk_bytes` or cannot be read.
    """
    filled: list[str] = []
    pieces: list[str] = []
    size = 0
    left_out = False
    for step in sorted(steps, key=lambda step: step["seq"]):
        text = step_text(step)
        if not text:
            left_out = left_out or text is None
            continue
        length = len(text.encode())
        if length > chunk_bytes:
            left_out = True
            continue
        if size + length > chunk_bytes:
            filled.append("".join(pieces))
            pieces, size = [], 0
        pieces.append(text)
        size += length
    if size:
        filled.append("".join(pieces))
    return filled, left_out


def request(model: str, text: str) -> Request:
    return {"model": model, "messages": [{"role": "user", "content": text}]}


def crossing(
    steps: Sequence[dict[str, Any]], count: Counter, *, model: str, chunk_bytes: int
) -> Count:
    """The tokens of the log's counted text, as `count` counts them for `model`.

    Each chunk is one request. Its value is its count less the count of the
    same request with one character, which is asked once. So what a request
    costs beyond its text is not counted, and the total is not over the
    text's own.
    """
    filled, left_out = chunks(steps, chunk_bytes)
    if not filled:
        return Count(total=0, chunks=0, partial=left_out)
    baseline = count(request(model, ONE_CHARACTER))
    total = sum(count(request(model, text)) - baseline for text in filled)
    return Count(total=total, chunks=len(filled), partial=left_out)


def counter(client: Any) -> Counter:
    """A counter over a Messages client: anything with `messages.count_tokens`."""

    def count(asked: Request) -> int:
        return int(client.messages.count_tokens(**asked).input_tokens)

    return count


# What the SDK reads from the process's environment besides the key: a log of
# every request, and another address. Neither is kept.
SDK_SETTINGS = ("ANTHROPIC_LOG", "ANTHROPIC_BASE_URL")


def keyed_client(env: Mapping[str, str]) -> Any:
    """The Messages client of a count on the Claude API, with the key of `env`.

    Only a run whose `LONG_JOB_PROVIDER` is `claude` gets one: on any other
    provider this raises before the SDK is imported. The SDK's own settings
    are dropped from the process first, and the address is the runtime's.
    A missing key raises KeyError with the variable's name, never a value.
    """
    if env.get("LONG_JOB_PROVIDER", "") != "claude":
        raise ValueError("a keyed client is built for a run on the Claude API only")
    key = env.get("CLAUDE_API_KEY", "")
    if not key:
        raise KeyError("CLAUDE_API_KEY")
    for name in SDK_SETTINGS:
        os.environ.pop(name, None)
    # Imported here, so loading this module imports neither.
    import anthropic

    from runtime.provider.messages import CLAUDE_API_URL

    return anthropic.Anthropic(api_key=key, base_url=CLAUDE_API_URL)


def failure(error: BaseException) -> dict[str, Any]:
    """A failed count, reduced to the error's type and its HTTP status. No text of it is kept."""
    status = getattr(error, "status_code", None)
    return {
        "type": type(error).__name__,
        "status": status if isinstance(status, int) and not isinstance(status, bool) else None,
    }
