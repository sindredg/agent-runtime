"""The API application: submit jobs, read them, follow their steps.

There is no authentication. The service listens on localhost.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.api.models import JobSubmission, JobView, StepList, StepView
from runtime.config import ApiConfig
from runtime.store.engine import create_engine
from runtime.store.jobs import create_job, get_job
from runtime.store.payload import InvalidPayload
from runtime.store.status import FINAL_STATUSES
from runtime.store.steps import read_progress

# Most steps one request or one poll of the event stream reads.
MAX_PAGE = 1000
# A comment line is sent after this many silent seconds, so proxies and
# clients do not take a quiet stream for a dead one.
KEEP_ALIVE_SECONDS = 15.0


def _event(name: str, data: str, event_id: int | None = None) -> bytes:
    lines = [f"id: {event_id}"] if event_id is not None else []
    lines += [f"event: {name}", f"data: {data}", "", ""]
    return "\n".join(lines).encode()


def create_app(
    config: ApiConfig | None = None, *, poll_interval: float = 0.5, page_size: int = MAX_PAGE
) -> FastAPI:
    """Build the application.

    `config` defaults to the environment, read when the application starts.
    `poll_interval` is how often an event stream looks for new steps.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config = config or ApiConfig.from_env()
        app.state.engine = create_engine(app.state.config.database_url)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="agent-runtime", lifespan=lifespan)

    def engine() -> AsyncEngine:
        found: AsyncEngine = app.state.engine
        return found

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> Response:
        """422 with what is wrong and where, without echoing the input back.

        The default handler includes the rejected input. Input that is not
        valid Unicode cannot be encoded into the response, and the request
        would end as a 500.
        """
        detail = [
            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return Response(
            # json.dumps escapes everything outside ASCII, so this always encodes.
            content=json.dumps({"detail": detail}, default=str),
            status_code=422,
            media_type="application/json",
        )

    @app.exception_handler(InvalidPayload)
    async def invalid_payload(request: Request, error: InvalidPayload) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": f"cannot be stored: {error}"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only: the process is up and serving. It does not check Postgres."""
        return {"status": "ok"}

    @app.post("/jobs", status_code=201)
    async def submit_job(submission: JobSubmission) -> JobView:
        settings: ApiConfig = app.state.config
        timeout = submission.timeout_seconds or settings.default_timeout_seconds
        async with engine().begin() as conn:
            job = await create_job(
                conn,
                spec=submission.spec(),
                token_budget=submission.token_budget or settings.default_token_budget,
                deadline=datetime.now(UTC) + timedelta(seconds=timeout),
            )
        return JobView.model_validate(job)

    @app.get("/jobs/{job_id}")
    async def read_job(job_id: UUID) -> JobView:
        async with engine().connect() as conn:
            job = await get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return JobView.model_validate(job)

    @app.get("/jobs/{job_id}/steps")
    async def read_job_steps(
        job_id: UUID,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
    ) -> StepList:
        """Steps with `seq` greater than `after`, in order, at most `limit`."""
        async with engine().connect() as conn:
            progress = await read_progress(conn, job_id, after_seq=after, limit=limit)
        if progress is None:
            raise HTTPException(status_code=404, detail="job not found")
        return StepList(steps=[StepView.model_validate(step) for step in progress[1]])

    @app.get("/jobs/{job_id}/events")
    async def follow_job(
        request: Request,
        job_id: UUID,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ) -> StreamingResponse:
        """Server-sent events: one `step` event per step, then one `end` event.

        Each step's event id is its `seq`. A client that lost the connection
        continues with `Last-Event-ID` or `?after=`. The stream ends when the
        job has a final status and every step was sent.
        """
        start = last_event_id if last_event_id is not None else after
        async with engine().connect() as conn:
            if await read_progress(conn, job_id, after_seq=start, limit=1) is None:
                raise HTTPException(status_code=404, detail="job not found")

        async def events() -> AsyncIterator[bytes]:
            last = start
            quiet_since = time.monotonic()
            while not await request.is_disconnected():
                async with engine().connect() as conn:
                    progress = await read_progress(conn, job_id, after_seq=last, limit=page_size)
                if progress is None:
                    return
                status, steps = progress
                for step in steps:
                    view = StepView.model_validate(step)
                    yield _event("step", view.model_dump_json(), step.seq)
                    last = step.seq
                if len(steps) == page_size:
                    # A full page: there may be more, so read on without waiting.
                    continue
                if status in FINAL_STATUSES:
                    yield _event("end", json.dumps({"status": status.value}))
                    return
                if steps:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since >= KEEP_ALIVE_SECONDS:
                    yield b": keep-alive\n\n"
                    quiet_since = time.monotonic()
                await asyncio.sleep(poll_interval)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return app


app = create_app()
