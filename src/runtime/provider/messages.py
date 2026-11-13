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
# Named here so that the client never falls back to ANTHROPIC_BASE_URL in the
# process environment: the caller's configuration alone decides.
CLAUDE_API_URL = "https://api.anthropic.com"


def _error_type(error: anthropic.APIStatusError) -> str | None:
    body = error.body
    detail = body.get("error") if isinstance(body, dict) else None
    kind = detail.get("type") if isinstance(detail, dict) else None
    return kind if isinstance(kind, str) else None


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
                if request.mid_stream is not None:
                    # Read event by event up to the first content block, tell
                    # the caller, and let the SDK collect the rest as usual.
                    async for event in stream:
                        if event.type == "content_block_start":
                            request.mid_stream()
                            break
                message = await stream.get_final_message()
        except anthropic.APIStatusError as error:
            status = error.status_code
            if status < 400:
                # An `error` event inside a stream that started with 200.
                # This is how overload is reported once output has begun.
                kind = _error_type(error)
                raise ProviderError(
                    f"provider sent an error during the stream: {kind}: {error.message}",
                    status=None,
                    retryable=kind in _RETRYABLE_ERROR_TYPES,
                ) from error
            raise ProviderError(
                f"provider answered {status}: {error.message}",
                status=status,
                retryable=status in _RETRYABLE_STATUSES or status >= 500,
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
            raise ProviderError(
                f"the provider's answer is not a message stream: {error!r}",
                status=None,
                retryable=True,
            ) from error
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
