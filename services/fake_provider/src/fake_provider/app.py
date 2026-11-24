"""The fake provider: scripted answers in the Anthropic Messages format.

`POST /v1/messages` picks a scenario by the request's `model` and a turn by
the number of assistant messages in the request. It answers as a stream of
server-sent events when the request sets `stream`, otherwise as one JSON
message. Every request is kept in a log that tests read back as the record
of what the provider was really asked.

`PUT /_fake/busy` makes it answer every request for one model, or for all,
with 429 `rate_limit_error` or 529 `overloaded_error` for a number of seconds,
the way the Claude API does when it is rate limited or overloaded.

`PUT /_fake/fault` makes the next N requests for one model fail in one named
way (`fake_provider.faults`). Each stays in the log with what it was answered.
"""

import asyncio
import hashlib
import json
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Body, FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from allowed_hosts import AllowedHosts
from fake_provider import faults
from fake_provider.faults import SPEND_CAP_DETAILS, BrokenStream, Fault
from fake_provider.messages import build_message, encode_sse, estimate_tokens, stream_events
from fake_provider.requests import RequestError, check_request, turn_index
from fake_provider.scenario import Scenario, load_scenarios

# The log keeps the newest entries and drops older ones. The cap is on the
# number of requests, not on their size.
REQUEST_LOG_SIZE = 10_000
# The longest busy window one call can set: one hour.
MAX_BUSY_SECONDS = 3600
# The names the stack and the tests reach it by. Any other Host is refused,
# so a web page that points its own name at 127.0.0.1 cannot use it.
HOSTS = ("fake-provider", "localhost", "127.0.0.1")
# The error type the Claude API sends with each busy status.
BUSY_ERRORS = {429: "rate_limit_error", 529: "overloaded_error"}


class Busy(BaseModel):
    """The body of `PUT /_fake/busy`."""

    model_config = ConfigDict(extra="forbid")

    seconds: float = Field(gt=0, le=MAX_BUSY_SECONDS, allow_inf_nan=False)
    status: Literal[429, 529] = 429
    # Sent as the `retry-after` header, in whole seconds. Left out, no header:
    # not every 429 of the Claude API carries one.
    retry_after: int | None = Field(default=None, ge=0, le=MAX_BUSY_SECONDS)
    # Only requests for this model are refused. Left out, every request is.
    model: str | None = None


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


def create_app(
    scenarios: dict[str, Scenario] | None = None, clock: Callable[[], float] = time.monotonic
) -> FastAPI:
    """The app. `clock` times the busy window; tests pass their own."""
    app = FastAPI(title="fake-provider")
    app.add_middleware(AllowedHosts, hosts=HOSTS)
    known = default_scenarios() if scenarios is None else scenarios
    log: deque[dict[str, Any]] = deque(maxlen=REQUEST_LOG_SIZE)
    counter = iter(range(1, 2**62))
    # The busy window: its settings, and when it ends on the monotonic clock.
    busy: list[tuple[Busy, float]] = []
    # The faults still to answer with, per model: the fault and how many are left.
    pending_faults: dict[str, tuple[Fault, int]] = {}

    def next_fault(body: Any) -> Fault | None:
        """The fault this request gets, counted off. None for a request without one."""
        model = body.get("model") if isinstance(body, dict) else None
        if not isinstance(model, str) or model not in pending_faults:
            return None
        fault, left = pending_faults[model]
        if left > 1:
            pending_faults[model] = (fault, left - 1)
        else:
            del pending_faults[model]
        return fault

    def busy_now(body: Any) -> Busy | None:
        """The busy window this request falls into, or None."""
        if not busy:
            return None
        window, ends = busy[0]
        if clock() >= ends:
            busy.clear()
            return None
        model = body.get("model") if isinstance(body, dict) else None
        return window if window.model in (None, model) else None

    def error(
        status: int,
        kind: str,
        message: str,
        request_id: str,
        details: dict[str, str] | None = None,
    ) -> Response:
        detail: dict[str, Any] = {"type": kind, "message": message}
        if details is not None:
            detail["details"] = details
        return _json(
            {"type": "error", "error": detail, "request_id": request_id},
            status,
        )

    async def answer_fault(fault: Fault, entry: dict[str, Any], model: str) -> Response:
        """The faulted answer. The log entry says what was answered before it is sent."""
        request_id, seq = entry["request_id"], entry["seq"]
        status, kind = fault.answered()
        entry.update(fault=fault.kind, status=status, error=kind, retry_after=fault.retry_after)
        headers = {"request-id": request_id}
        if fault.kind == "hold":
            assert fault.seconds is not None  # noqa: S101 - the validator requires it
            await asyncio.sleep(fault.seconds)
        if fault.kind in ("status", "hold", "spend_cap"):
            assert kind is not None  # noqa: S101 - these kinds all have an error type
            details = SPEND_CAP_DETAILS if fault.kind == "spend_cap" else None
            refused = error(status, kind, "the fake provider's fault", request_id, details)
            refused.headers.update(headers)
            if fault.retry_after is not None:
                refused.headers["retry-after"] = str(fault.retry_after)
            return refused
        if fault.kind == "not_a_stream":
            return Response(faults.NOT_A_STREAM, media_type="text/html", headers=headers)
        parts = faults.stream_parts(fault, model, seq)

        async def events() -> AsyncIterator[bytes]:
            for part in parts:
                yield part
            if fault.kind == "broken_stream":
                raise BrokenStream

        return StreamingResponse(events(), media_type="text/event-stream", headers=headers)

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
            name,
            scenario,
            turn,
            model=name,
            input_tokens=estimate_tokens(prompt),
            max_tokens=body["max_tokens"],
        )
        return name, turn, message, scenario.turns[turn].delay_seconds

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/messages")
    async def create_message(request: Request) -> Response:
        """A scripted answer, a counted fault, or the busy window's error while one is set."""
        seq = next(counter)
        request_id = f"req_fake_{seq:08d}"
        raw = await request.body()
        try:
            body = json.loads(raw)
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
            # The hash of the bytes received, so a test can show that a request
            # sent again is the same to the byte.
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }
        log.append(entry)
        # Before the busy window, so that a count of faults stays exact.
        if (fault := next_fault(body)) is not None:
            return await answer_fault(fault, entry, body["model"])
        if (window := busy_now(body)) is not None:
            kind = BUSY_ERRORS[window.status]
            entry.update(status=window.status, error=kind, retry_after=window.retry_after)
            refused = error(window.status, kind, "the fake provider is busy", request_id)
            refused.headers["request-id"] = request_id
            if window.retry_after is not None:
                refused.headers["retry-after"] = str(window.retry_after)
            return refused
        try:
            name, turn, message, delay = answer(body, request.headers)
        except RequestError as refused:
            entry.update(status=refused.status, error=refused.message)
            return error(refused.status, refused.kind, refused.message, request_id)
        # What the answer counts, so a test can add up a job's tokens from
        # the provider's side.
        entry.update(
            status=200,
            scenario=name,
            turn=turn,
            response_id=message["id"],
            usage={key: message["usage"][key] for key in ("input_tokens", "output_tokens")},
        )

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

    @app.put("/_fake/busy", status_code=204)
    async def set_busy(window: Annotated[Busy, Body()]) -> None:
        """Answer requests with the window's status from now for `seconds`. Replaces any window."""
        busy[:] = [(window, clock() + window.seconds)]

    @app.delete("/_fake/busy", status_code=204)
    async def clear_busy() -> None:
        busy.clear()

    @app.put("/_fake/fault", status_code=204)
    async def set_fault(fault: Annotated[Fault, Body()]) -> None:
        """Answer the next `count` requests for `model` with this fault. Replaces its last one."""
        pending_faults[fault.model] = (fault, fault.count)

    @app.delete("/_fake/fault", status_code=204)
    async def clear_faults() -> None:
        pending_faults.clear()

    return app


app = create_app()
