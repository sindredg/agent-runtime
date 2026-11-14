"""The API over real HTTP, against real Postgres.

The application runs in the test process on a free port, with a database
of its own and no worker. The tests write steps through the store, so they
decide exactly when each step appears.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.api.app import create_app
from runtime.config import ApiConfig
from runtime.store import held
from runtime.store.engine import create_engine
from runtime.store.jobs import Job, claim_job, create_job, get_job
from runtime.store.status import JobStatus
from runtime.store.steps import StepKind, append_step, read_steps


@dataclass
class Api:
    client: httpx2.AsyncClient
    engine: AsyncEngine
    # Comment lines seen on event streams, such as keep-alives.
    comments: list[str] = field(default_factory=list)

    async def new_job(self) -> Job:
        """A job that is `running`, as if a worker had claimed it."""
        async with self.engine.begin() as conn:
            await create_job(
                conn,
                spec={"prompt": "go"},
                token_budget=1000,
                deadline=datetime.now(UTC) + timedelta(hours=1),
            )
            job = await claim_job(conn, worker_id="test")
        assert job is not None
        return job

    async def add_steps(
        self, job: Job, first: int, count: int, payload: dict[str, Any] | None = None
    ) -> None:
        """Append `count` steps starting at seq `first`, each in its own transaction."""
        for seq in range(first, first + count):
            async with self.engine.begin() as conn:
                await append_step(
                    conn,
                    job_id=job.id,
                    seq=seq,
                    kind=StepKind.MODEL_REQUEST,
                    payload={"n": seq} if payload is None else payload,
                    lease_epoch=1,
                )

    async def finish(self, job: Job, status: JobStatus = JobStatus.SUCCEEDED) -> None:
        """Finish the job the way its worker does: a fenced move out of `running`.

        A failure brings its `error` step, as it does from a worker.
        """
        if status is JobStatus.FAILED:
            await held.fail(self.engine, job_id=job.id, lease_epoch=job.lease_epoch, error="boom")
            return
        await held.leave_running(
            self.engine, job_id=job.id, lease_epoch=job.lease_epoch, new_status=status
        )

    async def events(self, job_id: UUID, **request: Any) -> list[tuple[int | None, str, Any]]:
        """Read the event stream to its end: (id, event, data) for each event."""
        return [event async for event in self.follow(job_id, **request)]

    async def follow(
        self, job_id: UUID, **request: Any
    ) -> AsyncIterator[tuple[int | None, str, Any]]:
        url = f"/jobs/{job_id}/events"
        async with self.client.stream("GET", url, **request) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            fields: dict[str, str] = {}
            buffer = ""
            # Split at line feeds only, as the event stream format says.
            # aiter_lines would also split at U+2028 inside the data.
            async for chunk in response.aiter_text():
                buffer += chunk
                *lines, buffer = buffer.split("\n")
                for line in lines:
                    if line.startswith(":"):
                        self.comments.append(line)
                    elif line:
                        key, _, value = line.partition(": ")
                        fields[key] = value
                    elif fields:
                        event_id = int(fields["id"]) if "id" in fields else None
                        yield event_id, fields["event"], json.loads(fields["data"])
                        fields = {}


@asynccontextmanager
async def serving(database_url: str, **options: Any) -> AsyncIterator[Api]:
    config = ApiConfig(
        database_url=database_url,
        default_token_budget=5000,
        default_timeout_seconds=600,
        host="127.0.0.1",
        port=0,
    )
    app = create_app(config, poll_interval=0.02, **options)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, timeout_graceful_shutdown=2)
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        # uvicorn offers a flag to poll, not an event to wait on.
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    engine = create_engine(database_url)
    try:
        async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
            yield Api(client, engine)
    finally:
        await engine.dispose()
        server.should_exit = True
        await task


def api_test[T](database_url: str, body: Callable[[Api], Awaitable[T]], **options: Any) -> T:
    async def run() -> T:
        async with serving(database_url, **options) as api, asyncio.timeout(60):
            return await body(api)

    return asyncio.run(run())


# POST /jobs


def test_submit_stores_a_queued_job_and_returns_it(database_url: str) -> None:
    submission = {
        "prompt": "Read hello.txt.",
        "system": "Be brief.",
        "model": "hello",
        "files": {"hello.txt": "Hi\n"},
        "token_budget": 1234,
        "timeout_seconds": 90,
    }

    async def body(api: Api) -> tuple[httpx2.Response, Job | None, datetime]:
        before = datetime.now(UTC)
        response = await api.client.post("/jobs", json=submission)
        async with api.engine.connect() as conn:
            return response, await get_job(conn, UUID(response.json()["id"])), before

    response, stored, before = api_test(database_url, body)
    assert response.status_code == 201
    returned = response.json()
    assert stored is not None
    assert returned["status"] == "queued" == stored.status.value
    assert stored.spec == {
        "prompt": "Read hello.txt.",
        "system": "Be brief.",
        "model": "hello",
        "files": {"hello.txt": "Hi\n"},
    }
    assert returned["spec"] == stored.spec
    assert returned["token_budget"] == 1234 == stored.token_budget
    assert returned["tokens_used"] == 0
    assert returned["error"] is None
    assert datetime.fromisoformat(returned["deadline"]) == stored.deadline
    assert timedelta(seconds=89) < stored.deadline - before < timedelta(seconds=95)


def test_submit_applies_the_configured_defaults(database_url: str) -> None:
    async def body(api: Api) -> tuple[dict[str, Any], datetime]:
        before = datetime.now(UTC)
        return (await api.client.post("/jobs", json={"prompt": "go"})).json(), before

    job, before = api_test(database_url, body)
    assert job["spec"] == {"prompt": "go", "files": {}}
    assert job["token_budget"] == 5000
    remaining = datetime.fromisoformat(job["deadline"]) - before
    assert timedelta(seconds=599) < remaining < timedelta(seconds=605)


@pytest.mark.parametrize(
    "submission",
    [
        {},
        {"prompt": ""},
        {"prompt": "go", "surprise": 1},
        {"prompt": "go", "files": {"../x": "y"}},
        {"prompt": "go", "token_budget": 0},
        {"prompt": "go", "timeout_seconds": 0},
        {"prompt": "go", "timeout_seconds": 8 * 24 * 3600},
        {"prompt": "nul \x00 inside"},
        {"prompt": "go", "files": {"a.txt": "lone surrogate \ud800"}},
        {"prompt": "lone surrogate \ud800", "surprise": 1},
        # Larger than the column can hold, and values that only look like numbers.
        {"prompt": "go", "token_budget": 2**63},
        {"prompt": "go", "token_budget": "500"},
        {"prompt": "go", "token_budget": 5.0},
        {"prompt": "go", "timeout_seconds": True},
        # File sets that could not all be written.
        {"prompt": "go", "files": {"a": "1", "a/b": "2"}},
        {"prompt": "go", "files": {"a": "1", "./a": "2"}},
        {"prompt": "go", "files": {"a/": "1"}},
        {"prompt": "go", "files": {"n" * 3_000_000: "1"}},
        {"prompt": "x" * 1_000_001},
    ],
)
def test_submit_refuses_with_422_and_stores_nothing(
    database_url: str, submission: dict[str, Any]
) -> None:
    async def body(api: Api) -> tuple[httpx2.Response, Job | None]:
        # ensure_ascii keeps the surrogate as an escape, so the request is valid JSON.
        response = await api.client.post(
            "/jobs",
            content=json.dumps(submission),
            headers={"content-type": "application/json"},
        )
        async with api.engine.begin() as conn:
            return response, await claim_job(conn, worker_id="probe")

    response, any_job = api_test(database_url, body)
    assert response.status_code == 422
    assert response.json()["detail"]
    assert any_job is None


def test_the_largest_token_budget_the_column_holds_is_accepted(database_url: str) -> None:
    async def body(api: Api) -> httpx2.Response:
        return await api.client.post("/jobs", json={"prompt": "go", "token_budget": 2**63 - 1})

    response = api_test(database_url, body)
    assert response.status_code == 201
    assert response.json()["token_budget"] == 2**63 - 1


# GET /jobs/{id}


def test_read_job_returns_the_current_state(database_url: str) -> None:
    async def body(api: Api) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        submitted = (await api.client.post("/jobs", json={"prompt": "go"})).json()
        read = (await api.client.get(f"/jobs/{submitted['id']}")).json()
        async with api.engine.begin() as conn:
            claimed = await claim_job(conn, worker_id="w1")
        assert claimed is not None
        await held.fail(
            api.engine, job_id=claimed.id, lease_epoch=claimed.lease_epoch, error="boom"
        )
        return submitted, read, (await api.client.get(f"/jobs/{submitted['id']}")).json()

    submitted, read, later = api_test(database_url, body)
    assert read == submitted
    assert later["status"] == "failed"
    assert later["error"] == "boom"
    assert later["lease_epoch"] == 1


def test_unknown_job_is_404_on_every_job_route_and_a_bad_id_is_422(database_url: str) -> None:
    async def body(api: Api) -> dict[str, int]:
        return {
            path: (await api.client.get(path)).status_code
            for kind in ("", "/steps", "/events")
            for path in (f"/jobs/{uuid4()}{kind}", f"/jobs/not-a-uuid{kind}")
        }

    for path, status in api_test(database_url, body).items():
        assert status == (422 if "not-a-uuid" in path else 404), path


# GET /jobs/{id}/steps


def test_steps_are_returned_in_order_with_after_and_limit(database_url: str) -> None:
    async def body(api: Api) -> dict[str, Any]:
        job = await api.new_job()
        other = await api.new_job()
        await api.add_steps(other, 1, 2)
        empty = (await api.client.get(f"/jobs/{job.id}/steps")).json()
        await api.add_steps(job, 1, 5)
        get = api.client.get
        return {
            "empty": empty,
            "all": (await get(f"/jobs/{job.id}/steps")).json(),
            "after": (await get(f"/jobs/{job.id}/steps", params={"after": 3})).json(),
            "limit": (await get(f"/jobs/{job.id}/steps", params={"after": 1, "limit": 2})).json(),
            "past": (await get(f"/jobs/{job.id}/steps", params={"after": 5})).json(),
            "bad": (await get(f"/jobs/{job.id}/steps", params={"limit": 0})).status_code,
        }

    got = api_test(database_url, body)
    assert got["empty"] == {"steps": []}
    assert [step["seq"] for step in got["all"]["steps"]] == [1, 2, 3, 4, 5]
    assert got["all"]["steps"][2]["payload"] == {"n": 3}
    assert got["all"]["steps"][2]["kind"] == "model_request"
    assert set(got["all"]["steps"][0]) == {"seq", "kind", "payload", "lease_epoch", "created_at"}
    assert [step["seq"] for step in got["after"]["steps"]] == [4, 5]
    assert [step["seq"] for step in got["limit"]["steps"]] == [2, 3]
    assert got["past"] == {"steps": []}
    assert got["bad"] == 422


def test_a_seq_larger_than_the_column_holds_is_422_not_500(database_url: str) -> None:
    async def body(api: Api) -> dict[str, int]:
        job = await api.new_job()
        get = api.client.get
        too_big, largest = {"after": 2**31}, {"after": 2**31 - 1}
        return {
            "steps over": (await get(f"/jobs/{job.id}/steps", params=too_big)).status_code,
            "steps at": (await get(f"/jobs/{job.id}/steps", params=largest)).status_code,
            "events over": (await get(f"/jobs/{job.id}/events", params=too_big)).status_code,
            "header over": (
                await get(f"/jobs/{job.id}/events", headers={"Last-Event-ID": str(2**31)})
            ).status_code,
            "header junk": (
                await get(f"/jobs/{job.id}/events", headers={"Last-Event-ID": "abc"})
            ).status_code,
            "negative": (await get(f"/jobs/{job.id}/steps", params={"after": -1})).status_code,
        }

    assert api_test(database_url, body) == {
        "steps over": 422,
        "steps at": 200,
        "events over": 422,
        "header over": 422,
        "header junk": 422,
        "negative": 422,
    }


# GET /jobs/{id}/events


def test_event_stream_delivers_every_step_in_order_while_the_job_runs(database_url: str) -> None:
    total = 40

    async def body(api: Api) -> tuple[list[Any], list[float], float, list[Any]]:
        job = await api.new_job()
        arrivals: list[float] = []
        events = []

        async def reader() -> None:
            async for event in api.follow(job.id):
                arrivals.append(time.monotonic())
                events.append(event)

        reading = asyncio.create_task(reader())
        # Steps appear over time, one transaction each, in bursts of uneven size.
        seq = 1
        for burst in (1, 3, 1, 7, 2, 11, 5, 10):
            await api.add_steps(job, seq, burst)
            seq += burst
            await asyncio.sleep(0.03)
        assert seq == total + 1
        written = time.monotonic()
        await asyncio.sleep(0.1)
        await api.finish(job)
        await reading
        async with api.engine.connect() as conn:
            stored = await read_steps(conn, job.id)
        return events, arrivals, written, stored

    events, arrivals, written, stored = api_test(database_url, body)
    steps, end = events[:-1], events[-1]
    # Every step once, in order, no gap, and each event carries the stored step.
    assert [event_id for event_id, _, _ in steps] == list(range(1, total + 1))
    assert {name for _, name, _ in steps} == {"step"}
    assert [data["seq"] for _, _, data in steps] == list(range(1, total + 1))
    assert [data["payload"] for _, _, data in steps] == [step.payload for step in stored]
    assert end == (None, "end", {"status": "succeeded"})
    # Live: steps arrived while the job was still running, not all at the end.
    assert arrivals[0] < written
    assert arrivals[total - 1] < arrivals[-1]


def test_event_stream_reads_a_long_log_in_pages_without_a_gap(database_url: str) -> None:
    async def body(api: Api) -> list[Any]:
        job = await api.new_job()
        await api.add_steps(job, 1, 22)
        await api.finish(job, JobStatus.FAILED)
        return await api.events(job.id)

    # 23 steps, the last one the failure's own. With a page of 5 that is
    # four full pages, then a short one.
    events = api_test(database_url, body, page_size=5)
    assert [event_id for event_id, _, _ in events[:-1]] == list(range(1, 24))
    assert events[-1] == (None, "end", {"status": "failed"})


def test_event_stream_of_a_log_that_ends_on_a_page_boundary(database_url: str) -> None:
    async def body(api: Api) -> list[Any]:
        job = await api.new_job()
        await api.add_steps(job, 1, 10)
        await api.finish(job)
        return await api.events(job.id)

    events = api_test(database_url, body, page_size=5)
    assert [event_id for event_id, _, _ in events[:-1]] == list(range(1, 11))
    assert events[-1][1] == "end"


@pytest.mark.parametrize("resume", ["header", "query"])
def test_event_stream_continues_after_a_given_seq(database_url: str, resume: str) -> None:
    async def body(api: Api) -> list[Any]:
        job = await api.new_job()
        await api.add_steps(job, 1, 6)
        await api.finish(job)
        if resume == "header":
            return await api.events(job.id, headers={"Last-Event-ID": "4"})
        return await api.events(job.id, params={"after": 4})

    events = api_test(database_url, body)
    assert [event_id for event_id, _, _ in events] == [5, 6, None]


def test_event_stream_of_a_job_without_steps_ends_when_the_job_does(database_url: str) -> None:
    async def body(api: Api) -> tuple[list[Any], bool]:
        job = await api.new_job()
        reading = asyncio.create_task(api.events(job.id))
        await asyncio.sleep(0.2)
        still_open = not reading.done()
        await api.finish(job, JobStatus.CANCELLED)
        return await reading, still_open

    events, still_open = api_test(database_url, body)
    assert still_open
    assert events == [(None, "end", {"status": "cancelled"})]


def test_healthz(database_url: str) -> None:
    async def body(api: Api) -> httpx2.Response:
        return await api.client.get("/healthz")

    assert api_test(database_url, body).json() == {"status": "ok"}


def test_event_stream_carries_text_with_unicode_line_separators_intact(database_url: str) -> None:
    """U+2028, U+2029 and U+0085 inside a payload must not break an event apart.

    A client that splits lines the way Python does would cut a raw U+2028
    out of the middle of the data. The stream sends it as an escape.
    """
    text = "one\u2028two\u2029three\u0085four \U0001f600"

    async def body(api: Api) -> tuple[list[Any], str]:
        job = await api.new_job()
        await api.add_steps(job, 1, 1, {"text": text})
        await api.finish(job)
        raw = (await api.client.get(f"/jobs/{job.id}/events")).text
        return await api.events(job.id), raw

    events, raw = api_test(database_url, body)
    assert events[0][2]["payload"] == {"text": text}
    assert raw.isascii()
    assert len(raw.splitlines()) == len(raw.split("\n")) - 1


def test_a_quiet_event_stream_sends_keep_alive_comments(database_url: str) -> None:
    async def body(api: Api) -> tuple[list[Any], list[str]]:
        job = await api.new_job()
        reading = asyncio.create_task(api.events(job.id))
        await asyncio.sleep(0.5)
        await api.add_steps(job, 1, 1)
        await api.finish(job)
        return await reading, api.comments

    events, comments = api_test(database_url, body, keep_alive=0.1)
    assert comments
    assert set(comments) == {": keep-alive"}
    # The comments did not disturb the events around them.
    assert [(event_id, name) for event_id, name, _ in events] == [(1, "step"), (None, "end")]


# Approval


async def parked_for_approval(api: Api) -> Job:
    """A job waiting for a person, with the log a worker leaves: the intent and the question."""
    job = await api.new_job()
    asked = {
        "tool_use_id": "toolu_1",
        "name": "create_issue",
        "input": {"title": "CI is red"},
        "operation_id": f"{job.id}:toolu_1",
        "reason": "the tool's check could not tell whether the call happened",
    }
    log = [
        (StepKind.TOOL_INTENT, {"tool_use_id": "toolu_1", "name": "create_issue", "input": {}}),
        (StepKind.APPROVAL_REQUESTED, asked),
    ]
    for seq, (kind, payload) in enumerate(log, start=1):
        await held.append_step(
            api.engine, job_id=job.id, lease_epoch=1, seq=seq, kind=kind, payload=payload
        )
    return await held.leave_running(
        api.engine, job_id=job.id, lease_epoch=1, new_status=JobStatus.NEEDS_APPROVAL
    )


def test_event_stream_says_that_a_job_waits_for_approval_and_goes_on_when_it_is_given(
    database_url: str,
) -> None:
    """A job in `needs_approval` is not over, so the stream has no `end` to send.

    Without a word from the stream, a client that follows the job waits
    and cannot tell why.
    """

    async def body(api: Api) -> list[tuple[int | None, str, Any]]:
        job = await parked_for_approval(api)
        events = []
        async for event in api.follow(job.id):
            events.append(event)
            if event[1] == "status":
                # Long enough for the stream to say it again, if it were to.
                await asyncio.sleep(0.2)
                assert (await api.client.post(f"/jobs/{job.id}/approve")).status_code == 200
                async with api.engine.begin() as conn:
                    claimed = await claim_job(conn, worker_id="w2")
                assert claimed is not None
                await api.finish(claimed)
        return events

    events = api_test(database_url, body)
    assert [(event_id, name) for event_id, name, _ in events] == [
        (1, "step"),
        (2, "step"),
        # Said once, after the steps that led to it, and the stream stays open.
        (None, "status"),
        (3, "step"),
        (None, "end"),
    ]
    assert events[2][2] == {"status": "needs_approval"}
    assert events[3][2]["kind"] == "approval_given"
    assert events[4][2] == {"status": "succeeded"}


def test_approve_queues_the_job_and_records_the_approval_as_a_step(database_url: str) -> None:
    async def body(api: Api) -> tuple[Job, httpx2.Response, Job | None, list[Any]]:
        job = await parked_for_approval(api)
        response = await api.client.post(f"/jobs/{job.id}/approve")
        async with api.engine.begin() as conn:
            return job, response, await get_job(conn, job.id), await read_steps(conn, job.id)

    parked, response, job, steps = api_test(database_url, body)
    assert response.status_code == 200
    assert response.json()["id"] == str(parked.id)
    assert response.json()["status"] == "queued"
    assert job is not None
    assert (job.status, job.lease_owner, job.lease_epoch, job.takeovers) == (
        JobStatus.QUEUED,
        None,
        1,
        0,
    )
    assert [(step.seq, step.kind) for step in steps] == [
        (1, StepKind.TOOL_INTENT),
        (2, StepKind.APPROVAL_REQUESTED),
        (3, StepKind.APPROVAL_GIVEN),
    ]
    # The approval names the call it is for.
    assert steps[2].payload == {"tool_use_id": "toolu_1", "operation_id": f"{parked.id}:toolu_1"}


@pytest.mark.parametrize(
    "status",
    [status for status in JobStatus if status is not JobStatus.NEEDS_APPROVAL],
)
def test_approve_refuses_a_job_that_is_not_waiting_for_approval(
    database_url: str, status: JobStatus
) -> None:
    async def body(api: Api) -> tuple[httpx2.Response, Job | None, list[Any], list[Any]]:
        job = await api.new_job()
        if status is not JobStatus.RUNNING:
            await api.finish(job, status)
        async with api.engine.begin() as conn:
            before = await read_steps(conn, job.id)
        response = await api.client.post(f"/jobs/{job.id}/approve")
        async with api.engine.begin() as conn:
            return response, await get_job(conn, job.id), before, await read_steps(conn, job.id)

    response, job, before, steps = api_test(database_url, body)
    assert response.status_code == 409
    assert status.value in response.json()["detail"]
    assert job is not None
    assert job.status is status
    # No step was added. A failed job has the one its failure wrote.
    assert steps == before
    assert len(steps) == (1 if status is JobStatus.FAILED else 0)


def test_approve_of_an_unknown_job_is_404_and_of_a_bad_id_422(database_url: str) -> None:
    async def body(api: Api) -> tuple[httpx2.Response, httpx2.Response]:
        return (
            await api.client.post(f"/jobs/{uuid4()}/approve"),
            await api.client.post("/jobs/not-an-id/approve"),
        )

    unknown, bad = api_test(database_url, body)
    assert (unknown.status_code, unknown.json()) == (404, {"detail": "job not found"})
    assert bad.status_code == 422


def test_two_approvals_at_once_approve_once(database_url: str) -> None:
    """One question, one answer: the second approval finds the job already queued."""

    async def body(api: Api) -> tuple[list[int], list[Any]]:
        job = await parked_for_approval(api)
        responses = await asyncio.gather(
            *(api.client.post(f"/jobs/{job.id}/approve") for _ in range(5))
        )
        async with api.engine.begin() as conn:
            return [r.status_code for r in responses], await read_steps(conn, job.id)

    codes, steps = api_test(database_url, body)
    assert sorted(codes) == [200, 409, 409, 409, 409]
    assert [step.kind for step in steps].count(StepKind.APPROVAL_GIVEN) == 1
    assert [step.seq for step in steps] == [1, 2, 3]
