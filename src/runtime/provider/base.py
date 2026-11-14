"""What the runtime needs from a model provider, independent of any SDK."""

from collections.abc import Callable
from dataclasses import dataclass
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
    # Not sent. Called once when the first content of the answer has arrived
    # and the answer is not complete yet. Chaos tests stop the worker here.
    mid_stream: Callable[[], None] | None = None


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


class ProviderError(Exception):
    """A model call failed.

    `status` is the HTTP status, or None when no response arrived.
    `retryable` says whether the same request may succeed later: rate
    limits, overload, server errors, timeouts and broken connections.
    """

    def __init__(self, message: str, *, status: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Provider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Send the request and return the whole response, or raise ProviderError."""
        ...

    async def aclose(self) -> None: ...
