"""What the runtime needs from a model provider, independent of any SDK."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One call to the model, in Anthropic Messages terms."""

    model: str
    max_tokens: int
    messages: list[dict[str, Any]]
    system: str | None = None
    tools: tuple[dict[str, Any], ...] = ()
    # Sent as `metadata.user_id`. The runtime puts the job id here, so the
    # provider's own records can be matched to a job.
    user_id: str | None = None
    # Sent only when set. A summary request sets `{"type": "none"}`: the
    # tools stay in the request and the model may not call one (ADR 0033).
    tool_choice: dict[str, Any] | None = None
    # Not sent. Called once when the first content of the answer has arrived
    # and the answer is not complete yet. Chaos tests stop the worker here.
    mid_stream: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the model declined, as far as the provider said. Either field can be None."""

    category: str | None
    # Left out of the repr, so an exception or a log line that shows the
    # object does not carry the provider's words.
    explanation: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """The complete assistant message. `content` is kept exactly as received,
    because it is sent back unchanged in the next request."""

    id: str
    model: str
    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    # Set when `stop_reason` is `refusal`, and only then.
    refusal: Refusal | None = None
    # How many of `output_tokens` were thinking, when the provider reports it.
    # Already counted in `output_tokens`, so never added to the budget.
    thinking_tokens: int | None = None


class ProviderError(Exception):
    """A model call failed.

    `status` is the HTTP status, or None when no response arrived.
    `retryable` says whether the same request may succeed later: rate
    limits, overload, server errors, timeouts and broken connections. The
    job waits and sends it again, up to a cap (ADR 0030).
    `busy` says the provider refused the call with a 429 or 529, before any
    output: such a wait has no cap (ADR 0018). `retry_after` is the
    provider's `retry-after` header in seconds, None without one.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None,
        retryable: bool,
        busy: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.busy = busy
        self.retry_after = retry_after


class Provider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send the request and return the whole response, or raise ProviderError."""
        ...

    async def aclose(self) -> None: ...
