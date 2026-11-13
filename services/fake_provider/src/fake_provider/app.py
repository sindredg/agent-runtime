"""The fake provider: scripted answers in the Anthropic Messages format.

`POST /v1/messages` picks a scenario by the request's `model` and a turn by
the number of assistant messages in the request. It answers as a stream of
server-sent events when the request sets `stream`, otherwise as one JSON
message. Every request is kept in a log that tests read back as the record
of what the provider was really asked.
"""

import asyncio
import json
import os
from collections import deque
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from fake_provider.messages import build_message, encode_sse, estimate_tokens, stream_events
from fake_provider.requests import RequestError, check_request, turn_index
from fake_provider.scenario import Scenario, load_scenarios

# The log keeps the newest entries and drops older ones. The cap is on the
# number of requests, not on their size.
REQUEST_LOG_SIZE = 10_000


def _json(content: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    """A JSON response that always encodes.

    json.dumps escapes everything outside ASCII, so text that is not valid
    Unicode, which a request can carry, does not break the answer.
    """
    return Response(
        content=json.dumps(content),
        status_code=status,
        media_type="application/json",
        headers=headers,
    )


def stream_parts(message: dict[str, Any], delay: float) -> Iterator[bytes | float]:
    """The encoded events of a streamed answer, with the wait as a number among them.

    The wait comes after `message_start` and before the first content block,
    so a client that is stopped during it has an answer that began and did
    not finish.
    """
    waited = delay <= 0
    for event, data in stream_events(message):
        if not waited and event == "content_block_start":
            waited = True
            yield delay
        yield encode_sse(event, data)


def default_scenarios() -> dict[str, Scenario]:
    """Scenarios from FAKE_SCENARIO_DIR if set, otherwise the ones in the package."""
    directory = os.environ.get("FAKE_SCENARIO_DIR")
    if directory:
        return load_scenarios(Path(directory))
    with resources.as_file(resources.files("fake_provider") / "scenarios") as packaged:
        return load_scenarios(packaged)


def create_app(scenarios: dict[str, Scenario] | None = None) -> FastAPI:
    app = FastAPI(title="fake-provider")
    known = default_scenarios() if scenarios is None else scenarios
    log: deque[dict[str, Any]] = deque(maxlen=REQUEST_LOG_SIZE)
    counter = iter(range(1, 2**62))

    def error(status: int, kind: str, message: str, request_id: str) -> Response:
        return _json(
            {
                "type": "error",
                "error": {"type": kind, "message": message},
                "request_id": request_id,
            },
            status,
        )

    def answer(body: Any, headers: Any) -> tuple[str, int, dict[str, Any], float]:
        """The scenario name, turn, message and delay for a request, or RequestError."""
        if "x-api-key" not in headers and "authorization" not in headers:
            raise RequestError(401, "authentication_error", "x-api-key header is required")
        if "anthropic-version" not in headers:
            raise RequestError(400, "invalid_request_error", "anthropic-version header is required")
        messages = check_request(body)
        name = body["model"]
        scenario = known.get(name)
        if scenario is None:
            raise RequestError(404, "not_found_error", f"model: {name}")
        turn = turn_index(messages)
        if turn >= len(scenario.turns):
            raise RequestError(
                400,
                "invalid_request_error",
                f"scenario {name} has {len(scenario.turns)} turns;"
                f" the request asks for turn {turn + 1}",
            )
        prompt = {key: body.get(key) for key in ("system", "tools", "messages")}
        message = build_message(
            name, scenario, turn, model=name, input_tokens=estimate_tokens(prompt)
        )
        return name, turn, message, scenario.turns[turn].delay_seconds

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/messages")
    async def create_message(request: Request) -> Response:
        seq = next(counter)
        request_id = f"req_fake_{seq:08d}"
        try:
            body = json.loads(await request.body())
        # RecursionError: JSON nested deeper than the parser's limit.
        except ValueError, RecursionError:
            body = None
        metadata = body.get("metadata") if isinstance(body, dict) else None
        entry: dict[str, Any] = {
            "seq": seq,
            "request_id": request_id,
            "received_at": datetime.now(UTC).isoformat(),
            "user_id": metadata.get("user_id") if isinstance(metadata, dict) else None,
            "anthropic_version": request.headers.get("anthropic-version"),
            "body": body,
        }
        log.append(entry)
        try:
            name, turn, message, delay = answer(body, request.headers)
        except RequestError as refused:
            entry.update(status=refused.status, error=refused.message)
            return error(refused.status, refused.kind, refused.message, request_id)
        entry.update(status=200, scenario=name, turn=turn, response_id=message["id"])

        if not body.get("stream"):
            await asyncio.sleep(delay)
            return _json(message, headers={"request-id": request_id})

        async def events() -> AsyncIterator[bytes]:
            for part in stream_parts(message, delay):
                if isinstance(part, bytes):
                    yield part
                else:
                    await asyncio.sleep(part)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"request-id": request_id, "cache-control": "no-cache"},
        )

    @app.get("/_fake/requests")
    async def read_requests(user_id: str | None = None) -> Response:
        """The request log, oldest first. `user_id` filters on `metadata.user_id`."""
        return _json([entry for entry in log if user_id is None or entry["user_id"] == user_id])

    @app.delete("/_fake/requests", status_code=204)
    async def clear_requests() -> None:
        log.clear()

    return app


app = create_app()
