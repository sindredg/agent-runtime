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


def check_request(body: Any) -> list[dict[str, Any]]:
    """Refuse what the real API refuses outright. Returns the messages.

    Checked: the required fields, and that every assistant `tool_use` is
    answered by a `tool_result` in the next user message. A worker that
    loses a tool result fails here as it would against the real API.
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

    for position, message in enumerate(messages[:-1]):
        if message["role"] != "assistant":
            continue
        called = {b.get("id") for b in _blocks(message) if b.get("type") == "tool_use"}
        if not called:
            continue
        following = messages[position + 1]
        answered = (
            {b.get("tool_use_id") for b in _blocks(following) if b.get("type") == "tool_result"}
            if following["role"] == "user"
            else set()
        )
        missing = sorted(str(tool_use_id) for tool_use_id in called - answered)
        if missing:
            raise _invalid(
                f"messages.{position + 1}: tool_use ids were found without tool_result"
                f" blocks immediately after: {', '.join(missing)}"
            )
    return messages


def turn_index(messages: list[dict[str, Any]]) -> int:
    """Which turn the request asks for: the number of assistant messages it holds."""
    return sum(1 for message in messages if message["role"] == "assistant")
