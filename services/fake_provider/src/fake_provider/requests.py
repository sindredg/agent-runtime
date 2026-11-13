"""Checks on an incoming Messages request, and the turn it asks for."""

from typing import Any


class RequestError(Exception):
    """The request is refused. `status` and `kind` follow the API's error table."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message


def _invalid(message: str) -> RequestError:
    return RequestError(400, "invalid_request_error", message)


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """A message's content as blocks. A plain string is one text block."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list) and all(isinstance(block, dict) for block in content):
        return content
    raise _invalid("messages: content must be a string or a list of content blocks")


def _text_id(block: dict[str, Any], key: str) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid(f"{block.get('type')}.{key}: must be a non-empty string")
    return value


def check_request(body: Any) -> list[dict[str, Any]]:
    """Refuse what the real API refuses outright. Returns the messages.

    Checked: the required fields, and that tool calls and results pair up.
    Every assistant `tool_use` is answered by exactly one `tool_result` in
    the next user message, and no `tool_result` is without its `tool_use`.
    A worker that loses or repeats a tool result fails here as it would
    against the real API.
    """
    if not isinstance(body, dict):
        raise _invalid("the request body must be a JSON object")
    if not isinstance(body.get("model"), str) or not body["model"]:
        raise _invalid("model: field required")
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise _invalid("max_tokens: must be a positive integer")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _invalid("messages: at least one message is required")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise _invalid("messages: each message needs a role of user or assistant")
        _blocks(message)
    if messages[0]["role"] != "user":
        raise _invalid("messages: first message must use the user role")
    if messages[-1]["role"] != "user":
        raise _invalid("messages: the conversation must end with a user message")

    previous_calls: set[str] = set()
    for position, message in enumerate(messages):
        blocks = _blocks(message)
        if message["role"] == "assistant":
            calls = [_text_id(b, "id") for b in blocks if b.get("type") == "tool_use"]
            if len(set(calls)) != len(calls):
                raise _invalid(f"messages.{position}: tool_use ids must be unique")
            if previous_calls:
                # Two assistant messages in a row: the first one's tool
                # calls can no longer be answered.
                raise _invalid(
                    f"messages.{position}: tool_use ids were found without tool_result"
                    f" blocks immediately after: {', '.join(sorted(previous_calls))}"
                )
            previous_calls = set(calls)
            continue
        results = [_text_id(b, "tool_use_id") for b in blocks if b.get("type") == "tool_result"]
        if len(set(results)) != len(results):
            raise _invalid(f"messages.{position}: more than one tool_result for one tool_use id")
        unexpected = sorted(set(results) - previous_calls)
        if unexpected:
            raise _invalid(
                f"messages.{position}: unexpected tool_use_id in tool_result blocks:"
                f" {', '.join(unexpected)}. Each tool_result needs a tool_use in the"
                " previous message."
            )
        missing = sorted(previous_calls - set(results))
        if missing:
            raise _invalid(
                f"messages.{position}: tool_use ids were found without tool_result"
                f" blocks immediately after: {', '.join(missing)}"
            )
        previous_calls = set()
    return messages


def turn_index(messages: list[dict[str, Any]]) -> int:
    """Which turn the request asks for: the number of assistant messages it holds."""
    return sum(1 for message in messages if message["role"] == "assistant")
