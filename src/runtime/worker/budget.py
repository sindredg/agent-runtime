"""The token budget: an upper bound of a model call's input, checked before the call (ADR 0010).

The provider counts the input only when the answer arrives. So before each
call the worker bounds it from what it knows:

- the input and output tokens of the previous response: everything up to and
  including that response is already counted in tokens;
- plus the bytes of the text added to the conversation since, the tool
  results: a token is never shorter than one byte of the text it came from;
- plus a fixed allowance for what the provider adds around messages and tool
  definitions.

For the first call it is the bytes of the whole request plus the allowance.
A call whose bound does not fit into what is left is not made. Otherwise
`max_tokens` is cut to what is left after the bound, which bounds the output.
"""

from collections.abc import Sequence
from typing import Any

# Tokens the provider may add beyond the bytes it is sent: the system prompt
# it builds around tool definitions, and the markers around each message.
ALLOWANCE = 1024


def _bytes(value: Any) -> int:
    """The UTF-8 bytes of every key and value in `value`.

    The text itself, not its JSON: an escape such as `\\n` for a newline
    is how the request is written, not more text to read. The punctuation
    between the pieces is covered by the allowance.
    """
    if isinstance(value, str):
        return len(value.encode())
    if isinstance(value, dict):
        return sum(_bytes(key) + _bytes(item) for key, item in value.items())
    if isinstance(value, list | tuple):
        return sum(_bytes(item) for item in value)
    return len(str(value).encode())


def input_bound(
    *,
    system: str | None,
    tools: Sequence[dict[str, Any]],
    messages: Sequence[dict[str, Any]],
    counted: int | None,
    counted_messages: int,
) -> int:
    """An upper bound of the input tokens of a request with these parts.

    `counted` is the input plus output tokens of the last response, and
    `counted_messages` the length of the conversation it covers, its own
    message included. None before the first response.
    """
    if counted is None:
        request = {"system": system, "tools": list(tools), "messages": list(messages)}
        return _bytes(request) + ALLOWANCE
    return counted + _bytes(list(messages[counted_messages:])) + ALLOWANCE


def max_tokens_within(budget: int, used: int, bound: int, wanted: int) -> int | None:
    """`max_tokens` for the next call, or None when the call does not fit.

    The call needs room for its input bound and at least one output token.
    """
    left = budget - used - bound
    if left < 1:
        return None
    return min(wanted, left)
