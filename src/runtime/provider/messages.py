"""The provider client: the Anthropic Messages API through the official SDK.

The same client talks to the Claude API and to the fake provider. Only the
base URL differs, and that comes from configuration.
"""

import json
from typing import Any

import anthropic
import httpx2

from runtime.provider.base import ModelRequest, ModelResponse, ProviderError

# Statuses worth sending again later. Everything else is a fault in the request.
_RETRYABLE_STATUSES = frozenset({408, 409, 429})
# Error types the API can send as an event inside a stream that began with 200.
_RETRYABLE_ERROR_TYPES = frozenset(
    {"overloaded_error", "api_error", "rate_limit_error", "timeout_error"}
)
# The provider is busy: it refused the call with a 429 or 529. The job waits
# and tries again (ADR 0018). An error inside a stream is not counted as busy,
# whatever its type: output may have begun, so the call can be billed, and it
# falls under the caps on provider errors (ADR 0030).
_BUSY_STATUSES = frozenset({429, 529})
# A 429 for a spend cap. It has the rate limit's type and no `retry-after`,
# and waiting does not lift it.
_SPEND_CAP = "enforced_spend_limit_reached"
# The longest `retry-after` taken as it is: one day. A job's deadline is at
# most a week, and a larger value is more likely an error than a plan.
MAX_RETRY_AFTER = 24 * 3600.0
# Named here so that the client never falls back to ANTHROPIC_BASE_URL in the
# process environment: the caller's configuration alone decides.
CLAUDE_API_URL = "https://api.anthropic.com"


def _error_detail(error: anthropic.APIStatusError) -> dict[str, Any]:
    body = error.body
    detail = body.get("error") if isinstance(body, dict) else None
    return detail if isinstance(detail, dict) else {}


def _error_type(error: anthropic.APIStatusError) -> str | None:
    kind = _error_detail(error).get("type")
    return kind if isinstance(kind, str) else None


def _spend_cap(error: anthropic.APIStatusError) -> bool:
    details = _error_detail(error).get("details")
    return isinstance(details, dict) and details.get("error_code") == _SPEND_CAP


def retry_after(value: str | None) -> float | None:
    """The `retry-after` header in seconds, or None when it is missing or not a number.

    The Claude API sends whole seconds. The HTTP-date form is not used by it
    and is not read. A value over MAX_RETRY_AFTER is cut to it.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    if not 0 <= seconds < float("inf"):
        return None
    return min(seconds, MAX_RETRY_AFTER)


def _not_a_stream(error: Exception) -> ProviderError:
    return ProviderError(
        f"the provider's answer is not a message stream: {error!r}",
        status=None,
        retryable=True,
    )


class AnthropicProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url or CLAUDE_API_URL,
            # The SDK would retry on its own. The runtime decides about
            # retries itself, so that each attempt is visible in the step log.
            max_retries=0,
            http_client=http_client,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        optional: dict[str, Any] = {}
        if request.system is not None:
            optional["system"] = request.system
        if request.tools:
            optional["tools"] = list(request.tools)
        if request.user_id is not None:
            optional["metadata"] = {"user_id": request.user_id}
        try:
            # Streamed, so a long response does not run into an HTTP timeout.
            # The SDK collects the events into one message.
            async with self._client.messages.stream(
                model=request.model,
                max_tokens=request.max_tokens,
                messages=request.messages,  # type: ignore[arg-type]
                **optional,
            ) as stream:
                try:
                    if request.mid_stream is not None:
                        # Read event by event up to the first content block, tell
                        # the caller, and let the SDK collect the rest as usual.
                        async for event in stream:
                            if event.type == "content_block_start":
                                request.mid_stream()
                                break
                    message = await stream.get_final_message()
                except (IndexError, AttributeError) as error:
                    # Events the SDK cannot put together. Caught only here, so
                    # a fault in building the request is not taken for a bad
                    # answer and sent again.
                    raise _not_a_stream(error) from error
        except anthropic.APIStatusError as error:
            status = error.status_code
            if status < 400:
                # An `error` event inside a stream that started with 200.
                # This is how overload is reported once output has begun. A
                # spend cap is final here too.
                kind = _error_type(error)
                capped = _spend_cap(error)
                raise ProviderError(
                    f"provider sent an error during the stream: {kind}: {error.message}",
                    status=None,
                    retryable=not capped and kind in _RETRYABLE_ERROR_TYPES,
                ) from error
            capped = status == 429 and _spend_cap(error)
            raise ProviderError(
                f"provider answered {status}: {error.message}",
                status=status,
                retryable=not capped and (status in _RETRYABLE_STATUSES or status >= 500),
                busy=not capped and status in _BUSY_STATUSES,
                retry_after=retry_after(error.response.headers.get("retry-after")),
            ) from error
        except (anthropic.APIError, httpx2.HTTPError) as error:
            # Timeouts, a connection that broke during the stream, a response
            # the SDK could not validate.
            raise ProviderError(
                f"no complete response from the provider: {error!r}", status=None, retryable=True
            ) from error
        except (AssertionError, json.JSONDecodeError) as error:
            # What the SDK raises for a 200 that is not a message stream: an
            # empty body, other content, or a line that is not JSON. The
            # answer was bad, not the request, so it may be sent again.
            raise _not_a_stream(error) from error
        except (ValueError, TypeError, RuntimeError) as error:
            # The SDK refused the request or could not use the response for
            # a reason that will not change on a second try.
            raise ProviderError(
                f"the model call could not be made: {error!r}", status=None, retryable=False
            ) from error

        if message.stop_reason is None:
            # The stream ended without the event that carries the stop
            # reason. What arrived is a fragment and is thrown away.
            raise ProviderError(
                "the response stream ended before the message was complete",
                status=None,
                retryable=True,
            )
        body = message.to_dict(mode="json")
        content = body["content"]
        if not isinstance(content, list):
            raise ProviderError("the response has no content list", status=None, retryable=False)
        return ModelResponse(
            id=message.id,
            model=message.model,
            content=content,
            stop_reason=message.stop_reason,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

    async def aclose(self) -> None:
        await self._client.close()
