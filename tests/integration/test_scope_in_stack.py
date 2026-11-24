"""AR-020 in the stack: a scoped command cannot reach the stack's own Postgres.

The job goes through the API to the stack's worker, with the worker's own
deny list: the guards' network and the runtime's (`SANDBOX_DENY_NETWORKS`).
Its scope names `postgres:5432`. The worker resolves the name on its own
network, finds the address in the deny list, and starts no call.
"""

import asyncio
import time
from typing import Any

import asyncpg
import httpx2

from runtime.logs import text_safe

TIMEOUT_SECONDS = 60
FINAL = {"succeeded", "failed", "cancelled"}


def steps_of(database_url: str, job_id: str) -> list[Any]:
    async def read() -> list[Any]:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            return await conn.fetch(
                "SELECT seq, kind, payload::text AS payload FROM steps"
                " WHERE job_id = $1 ORDER BY seq",
                job_id,
            )
        finally:
            await conn.close()

    return asyncio.run(read())


def test_a_scope_that_names_the_stacks_postgres_starts_no_call(
    api_url: str, fake_provider_url: str, stack_database_url: str
) -> None:
    spec = {
        "prompt": "Connect to the database.",
        "model": "scope-postgres",
        "scope": [{"host": "postgres", "ports": [5432]}],
    }
    submitted = httpx2.post(f"{api_url}/jobs", json=spec, timeout=10)
    submitted.raise_for_status()
    job_id = submitted.json()["id"]
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while (job := httpx2.get(f"{api_url}/jobs/{job_id}", timeout=10).json())["status"] not in FINAL:
        assert time.monotonic() < deadline, f"job still {job['status']}"
        time.sleep(0.2)
    steps = steps_of(stack_database_url, job_id)
    kinds = [step["kind"] for step in steps]
    results = [step["payload"] for step in steps if step["kind"] == "tool_result"]
    print(text_safe(f"\n{job['status']} {kinds} {results}"))

    # No command ran: no intent was written, so no container was started.
    assert "tool_intent" not in kinds
    assert len(results) == 1
    assert "'postgres'" in results[0]
    assert "is in the deny list" in results[0]
    assert '"is_error": true' in results[0]
    assert "connected" not in results[0]
    # The refusal is a tool result: the model was told, and the job went on.
    assert job["status"] == "succeeded", job["error"]
    log = httpx2.get(
        f"{fake_provider_url}/_fake/requests", params={"user_id": job_id}, timeout=10
    ).json()
    assert [entry["turn"] for entry in log] == [0, 1]
