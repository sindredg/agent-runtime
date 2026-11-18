"""G5 for one job, through the whole stack: a rate limit slows the job, it does not fail it.

The pass line is fixed before the run (2b task 8): the fake answers 429 with
`retry-after: 5` for 60 s, starting before one `ci-triage` job is submitted.
The job ends `succeeded` with the tool calls of a healthy run, it was in
`waiting` with `not_before` set, no request of it reaches the fake earlier
than 5 s after a 429 to it, and the fake refuses at most 6 of its requests.

The fake's request log is the record of what the worker sent. The shared
limiter across workers and the 50-job run stay with G5 in milestone 3.
"""

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import asyncpg
import httpx2
import pytest

from runtime.worker.backoff import floor

from .conftest import REPO_ROOT

CI_TRIAGE = json.loads((REPO_ROOT / "examples" / "ci-triage.json").read_text())
MODEL = CI_TRIAGE["model"]
WINDOW_SECONDS = 60
RETRY_AFTER = 5
# From the floors with `retry-after: 5`: 5, 5, 5, 8, 16, 30. The seventh
# request comes 69 s after the first or later, past the window.
MOST_REFUSED = 6
# The window, the longest last wait (30 s plus up to 30 s of jitter), and
# the run itself. A job still open then has not recovered.
TIMEOUT_SECONDS = WINDOW_SECONDS + 60 + 60
FINAL = {"succeeded", "failed", "cancelled"}


@pytest.fixture
def fake(fake_provider_url: str) -> Iterator[str]:
    yield fake_provider_url
    httpx2.delete(f"{fake_provider_url}/_fake/busy", timeout=10)


def submit(api_url: str) -> str:
    response = httpx2.post(f"{api_url}/jobs", json=CI_TRIAGE, timeout=10)
    assert response.status_code == 201, response.text
    job_id: str = response.json()["id"]
    return job_id


def follow(api_url: str, job_id: str, timeout: float) -> tuple[str, list[dict[str, Any]]]:
    """Poll the job until it is final. Returns its status and every state seen."""
    seen: list[dict[str, Any]] = []
    ends = time.monotonic() + timeout
    while time.monotonic() < ends:
        job = httpx2.get(f"{api_url}/jobs/{job_id}", timeout=10).json()
        seen.append({key: job[key] for key in ("status", "not_before")})
        if job["status"] in FINAL:
            return job["status"], seen
        time.sleep(0.5)
    return seen[-1]["status"], seen


def steps_of(database_url: str, job_id: str) -> list[tuple[str, dict[str, Any]]]:
    async def read() -> list[tuple[str, dict[str, Any]]]:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            rows = await conn.fetch(
                "SELECT kind, payload FROM steps WHERE job_id = $1 ORDER BY seq", job_id
            )
        finally:
            await conn.close()
        return [(row["kind"], json.loads(row["payload"])) for row in rows]

    return asyncio.run(read())


def tool_calls(steps: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, Any]]:
    return [(p["name"], p["input"]) for kind, p in steps if kind == "tool_intent"]


def requests_of(fake_provider_url: str, job_id: str) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = httpx2.get(
        f"{fake_provider_url}/_fake/requests", params={"user_id": job_id}, timeout=10
    ).json()
    return log


def test_a_rate_limited_job_waits_and_succeeds(
    api_url: str, fake: str, stack_database_url: str
) -> None:
    # A healthy run first: the tool calls the slowed job has to make too.
    healthy = submit(api_url)
    status, _ = follow(api_url, healthy, timeout=60)
    assert status == "succeeded"
    healthy_calls = tool_calls(steps_of(stack_database_url, healthy))
    assert [name for name, _ in healthy_calls] == ["read_file", "create_issue"]

    busy = {"model": MODEL, "seconds": WINDOW_SECONDS, "retry_after": RETRY_AFTER, "status": 429}
    assert httpx2.put(f"{fake}/_fake/busy", json=busy, timeout=10).status_code == 204
    job_id = submit(api_url)
    status, seen = follow(api_url, job_id, timeout=TIMEOUT_SECONDS)
    steps = steps_of(stack_database_url, job_id)
    requests = requests_of(fake, job_id)
    refused = [i for i, entry in enumerate(requests) if entry["status"] == 429]
    print(
        f"\nG5 local: status={status} requests={len(requests)} refused={len(refused)}"
        f" waits={[p['delay_seconds'] for k, p in steps if k == 'status_changed']}"
    )

    # Zero failed jobs, and the same work as a healthy run.
    assert status == "succeeded"
    assert tool_calls(steps) == healthy_calls

    # It waited, with `not_before` set, once for each 429.
    assert any(s["status"] == "waiting" and s["not_before"] for s in seen)
    waits = [p for kind, p in steps if kind == "status_changed" and p["to"] == "waiting"]
    assert len(waits) == len(refused) >= 1
    assert all(p["not_before"] and p["retry_after"] == RETRY_AFTER for p in waits)
    assert [p["attempt"] for p in waits] == list(range(1, len(waits) + 1))

    # The fake's own log: no request came back sooner than `retry-after`,
    # and the window refused no more than the floors allow.
    assert 1 <= len(refused) <= MOST_REFUSED
    for i in refused:
        gap = datetime.fromisoformat(requests[i + 1]["received_at"]) - datetime.fromisoformat(
            requests[i]["received_at"]
        )
        assert gap.total_seconds() >= floor(1, RETRY_AFTER) == RETRY_AFTER
    assert [entry["status"] for entry in requests[len(refused) :]] == [200, 200, 200]
