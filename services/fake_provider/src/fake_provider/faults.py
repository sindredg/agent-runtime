"""Faults the fake provider answers with, counted per request.

The busy window is timed and says the provider is busy. A fault is counted:
`PUT /_fake/fault` makes the next `count` requests for one model fail in one
named way, so a test knows exactly which requests failed and how.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fake_provider.messages import encode_sse, stream_events

# The most requests one call can fault.
MAX_FAULT_COUNT = 1000
# The longest a request can be held: past any client timeout a test sets.
MAX_HOLD_SECONDS = 60
# What a `retry-after` on a fault can say, in whole seconds.
MAX_RETRY_AFTER = 3600
# The error type the Claude API sends with each status.
STATUS_ERRORS = {
    400: "invalid_request_error",
    408: "timeout_error",
    409: "conflict_error",
    500: "api_error",
    502: "api_error",
    503: "api_error",
    504: "timeout_error",
}
SPEND_CAP_DETAILS = {"error_code": "enforced_spend_limit_reached"}
NOT_A_STREAM = "<html>a proxy's page</html>"

FaultKind = Literal[
    # An error answer with `status`.
    "status",
    # A 200 stream with some output, then an `error` event of type `error`.
    "stream_error",
    # A 200 stream with some output, then the connection breaks.
    "broken_stream",
    # A 200 stream that ends before the event with the stop reason.
    "no_stop_reason",
    # A 200 whose body is not a message stream.
    "not_a_stream",
    # No answer for `seconds`, then a 504. A client timeout comes first, and
    # the log says 504 whether or not the client was still there for it.
    "hold",
    # The spend cap, as a 429 and as an event inside a stream.
    "spend_cap",
    "stream_spend_cap",
]


class BrokenStream(Exception):
    """Raised inside a streamed answer, so the server drops the connection."""


class Fault(BaseModel):
    """The body of `PUT /_fake/fault`."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    count: int = Field(ge=1, le=MAX_FAULT_COUNT)
    kind: FaultKind
    status: Literal[400, 408, 409, 500, 502, 503, 504] | None = None
    error: Literal["api_error", "timeout_error"] | None = None
    retry_after: int | None = Field(default=None, ge=0, le=MAX_RETRY_AFTER)
    seconds: float | None = Field(default=None, gt=0, le=MAX_HOLD_SECONDS, allow_inf_nan=False)

    @model_validator(mode="after")
    def _fields_fit_the_kind(self) -> Self:
        needed = {"status": "status", "stream_error": "error", "hold": "seconds"}.get(self.kind)
        for field in ("status", "error", "seconds"):
            if (getattr(self, field) is not None) != (field == needed):
                raise ValueError(f"kind {self.kind} does not fit field {field}")
        if self.retry_after is not None and self.kind != "status":
            raise ValueError("only a status fault has a retry-after")
        return self

    def answered(self) -> tuple[int, str | None]:
        """The status and error type this fault answers with, as the log records them."""
        match self.kind:
            case "status":
                assert self.status is not None  # noqa: S101 - the validator requires it
                return self.status, STATUS_ERRORS[self.status]
            case "stream_error":
                return 200, self.error
            case "hold":
                return 504, "timeout_error"
            case "spend_cap":
                return 429, "rate_limit_error"
            case "stream_spend_cap":
                return 200, "rate_limit_error"
            case _:
                return 200, None


def _message(model: str, seq: int) -> dict[str, Any]:
    """The message a faulted stream starts to deliver. It never arrives whole."""
    text = "Part of an answer that does not arrive."
    return {
        "id": f"msg_fake_fault_{seq:08d}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1,
            "output_tokens": 8,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def stream_parts(fault: Fault, model: str, seq: int) -> list[bytes]:
    """The events of a faulted stream, up to the point where it goes wrong."""
    events = list(stream_events(_message(model, seq)))
    if fault.kind == "no_stop_reason":
        end = next(i for i, (name, _) in enumerate(events) if name == "message_delta")
        return [encode_sse(name, data) for name, data in events[:end]]
    first_delta = next(i for i, (name, _) in enumerate(events) if name == "content_block_delta")
    parts = [encode_sse(name, data) for name, data in events[: first_delta + 1]]
    if fault.kind in ("stream_error", "stream_spend_cap"):
        _, kind = fault.answered()
        detail: dict[str, Any] = {"type": kind, "message": "the fake provider's fault"}
        if fault.kind == "stream_spend_cap":
            detail["details"] = SPEND_CAP_DETAILS
        parts.append(encode_sse("error", {"type": "error", "error": detail}))
    return parts
