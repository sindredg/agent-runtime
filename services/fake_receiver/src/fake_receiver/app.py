"""The fake notification receiver: where the workers post a parked job's question.

It keeps every POST it is sent, in arrival order, with its own clock's time
and the status it answered. Repeats are kept as they came. Tests read this
log as the record of what reached the operator, independent of the
runtime's step log.

By default every POST is answered 200. A test can queue the next answers:
an error status, a redirect, a wait before answering, or a body that never
ends. A queued answer is used up by one POST.

Everything is kept in memory and is gone when the service stops.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from allowed_hosts import AllowedHosts

# Bytes of a POST body read, POSTs kept, and answers that can be queued.
# They bound the memory a test run can use up: about 640 MiB at most.
MAX_BODY_BYTES = 64 * 1024
MAX_ARRIVALS = 10_000
MAX_QUEUED = 1000
# Seconds a queued answer can wait: past the sender's 10 s deadline.
MAX_HANG_SECONDS = 120.0
# The names the stack and the tests reach it by. Any other Host is refused,
# so a web page that points its own name at 127.0.0.1 cannot use it.
HOSTS = ("fake-receiver", "localhost", "127.0.0.1")
# Where a queued redirect points: back here, so a sender that follows it
# shows up as a second arrival.
PATH = "/notifications"


class Answer(BaseModel):
    """How to answer one POST."""

    model_config = ConfigDict(extra="forbid", strict=True)

    status: int = Field(default=200, ge=200, le=599)
    # Seconds to wait after the arrival is logged, before answering.
    hang: float = Field(default=0.0, ge=0.0, le=MAX_HANG_SECONDS)
    # Send a body that never ends, until the sender closes the connection.
    endless_body: bool = False


class Answers(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    answers: list[Answer] = Field(min_length=1, max_length=MAX_QUEUED)


def _json(content: Any, status: int = 200) -> Response:
    # json.dumps escapes everything outside ASCII, so text that is not valid
    # Unicode, which a request can carry, does not break the answer.
    return Response(content=json.dumps(content), status_code=status, media_type="application/json")


async def _endless() -> AsyncIterator[bytes]:
    chunk = b"x" * 65536
    while True:
        yield chunk
        await asyncio.sleep(0.001)


async def _read(request: Request) -> bytes | None:
    """The POST's body, or None when it is longer than MAX_BODY_BYTES. Never reads past that."""
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            return None
    return raw


def _body(raw: bytes | None) -> Any:
    """The POST's JSON, or None when it is not JSON. Kept as it came, not checked."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def create_app() -> FastAPI:
    app = FastAPI(title="fake-receiver")
    app.add_middleware(AllowedHosts, hosts=HOSTS)
    arrivals: list[dict[str, Any]] = []
    queued: deque[Answer] = deque()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> Response:
        """422 with what is wrong and where, without echoing the input back."""
        detail = [
            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return Response(
            content=json.dumps({"detail": detail}, default=str),
            status_code=422,
            media_type="application/json",
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(PATH)
    async def receive(request: Request) -> Response:
        """Log the POST, then answer it as the next queued answer says, else 200."""
        if len(arrivals) >= MAX_ARRIVALS:
            return _json({"detail": "the log is full; clear it with DELETE /notifications"}, 507)
        raw = await _read(request)
        answer = queued.popleft() if queued else Answer()
        arrivals.append(
            {
                "received_at": datetime.now(UTC).isoformat(),
                "body": _body(raw),
                "content_type": request.headers.get("content-type"),
                "status": answer.status,
                "hang": answer.hang,
            }
        )
        if answer.hang:
            await asyncio.sleep(answer.hang)
        if answer.endless_body:
            return StreamingResponse(_endless(), status_code=answer.status)
        if 300 <= answer.status < 400:
            return Response(status_code=answer.status, headers={"location": PATH})
        return Response(status_code=answer.status)

    @app.get(PATH)
    async def list_arrivals() -> Response:
        """Every POST, oldest first."""
        return _json(arrivals)

    @app.delete(PATH, status_code=204)
    async def clear() -> None:
        """Forget every POST and every queued answer."""
        arrivals.clear()
        queued.clear()

    @app.post("/_fake/answers")
    async def queue_answers(new: Answers) -> Response:
        """Queue answers for the next POSTs, in order."""
        if len(queued) + len(new.answers) > MAX_QUEUED:
            return _json({"detail": f"at most {MAX_QUEUED} answers can wait"}, 507)
        queued.extend(new.answers)
        return Response(status_code=204)

    return app


app = create_app()
