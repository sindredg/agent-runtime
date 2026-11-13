"""The provider client: the Anthropic Messages API through the official SDK.

The same client talks to the Claude API and to the fake provider. Only the
base URL differs, and that comes from configuration.
"""

from typing import Any

import anthropic
import httpx2

from runtime.provider.base import ModelRequest, ModelResponse, ProviderError

# Statuses worth sending again later. Everything else is a fault in the request.
_RETRYABLE_STATUSES = frozenset({408, 409, 429})


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
            # None means the SDK's default, the Claude API.
            base_url=base_url,
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
                message = await stream.get_final_message()
        except anthropic.APIStatusError as error:
            status = error.status_code
            raise ProviderError(
                f"provider answered {status}: {error.message}",
                status=status,
                retryable=status in _RETRYABLE_STATUSES or status >= 500,
            ) from error
        except anthropic.APIConnectionError as error:
            # Includes timeouts and a connection that broke during the stream.
            raise ProviderError(
                f"no complete response from the provider: {error}", status=None, retryable=True
            ) from error
        except httpx2.HTTPError as error:
            raise ProviderError(
                f"no complete response from the provider: {error!r}", status=None, retryable=True
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
