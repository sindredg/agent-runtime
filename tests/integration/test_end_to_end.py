"""One job through the whole stack, started the way a user starts it.

`runtime submit examples/hello.json --follow` runs as a real command
against the compose stack: API, Postgres, worker, fake provider. The result
is then checked in three places that do not depend on each other: the
command's output, the database, and the fake provider's request log.
"""

import asyncio
import json
import os
import subprocess
import sys
from typing import Any

import asyncpg
import httpx2

from .conftest import REPO_ROOT

EXAMPLE = REPO_ROOT / "examples" / "hello.json"
EXPECTED_KINDS = [
    "model_request",
    "model_response",
    "tool_intent",
    "tool_result",
    "model_request",
    "model_response",
]
# Long enough for a healthy stack, short enough that a stack without a
# worker fails the test instead of hanging it.
TIMEOUT_SECONDS = 30


def runtime(api_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed command, arguments come from the test
        [sys.executable, "-m", "runtime.cli", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "RUNTIME_API_URL": api_url},
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )


def database_rows(database_url: str, job_id: str) -> tuple[Any, list[Any]]:
    async def read() -> tuple[Any, list[Any]]:
        conn = await asyncpg.connect(database_url, timeout=5)
        try:
            job = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1", job_id)
            steps = await conn.fetch(
                "SELECT seq, kind, payload, lease_epoch FROM steps WHERE job_id = $1 ORDER BY seq",
                job_id,
            )
        finally:
            await conn.close()
        return job, steps

    return asyncio.run(read())


def test_hello_job_runs_to_success(
    api_url: str, fake_provider_url: str, stack_database_url: str
) -> None:
    file_text = json.loads(EXAMPLE.read_text())["files"]["hello.txt"]

    submitted = runtime(api_url, "submit", str(EXAMPLE), "--follow")
    assert submitted.returncode == 0, submitted.stderr
    job_id, *step_lines, outcome = submitted.stdout.splitlines()

    # 1. What the command printed.
    assert [line.split()[1] for line in step_lines] == EXPECTED_KINDS
    assert [int(line.split()[0]) for line in step_lines] == [1, 2, 3, 4, 5, 6]
    assert outcome.startswith("succeeded")

    # 2. What is in the database, read with plain SQL.
    job, steps = database_rows(stack_database_url, job_id)
    assert job["status"] == "succeeded"
    assert job["error"] is None
    assert job["lease_owner"] is None
    assert job["lease_epoch"] == 1
    assert job["tokens_used"] > 0
    assert [(row["seq"], row["kind"]) for row in steps] == list(enumerate(EXPECTED_KINDS, start=1))
    assert {row["lease_epoch"] for row in steps} == {1}
    result = json.loads(steps[3]["payload"])
    assert result["content"] == file_text
    assert result["is_error"] is False

    # 3. What the provider was really asked: two requests for this job, and
    # the second carries the content of the file the tool read.
    log = httpx2.get(
        f"{fake_provider_url}/_fake/requests", params={"user_id": job_id}, timeout=10
    ).json()
    assert [(entry["status"], entry["turn"]) for entry in log] == [(200, 0), (200, 1)]
    assert [tool["name"] for tool in log[0]["body"]["tools"]] == ["read_file", "create_issue"]
    sent_result = log[1]["body"]["messages"][2]["content"][0]
    assert sent_result["type"] == "tool_result"
    assert sent_result["content"] == file_text

    # The other two commands agree with it.
    status = runtime(api_url, "status", job_id)
    assert status.returncode == 0
    assert "status:       succeeded" in status.stdout
    logs = runtime(api_url, "logs", job_id)
    assert logs.returncode == 0
    assert logs.stdout.splitlines() == step_lines


CI_TRIAGE = REPO_ROOT / "examples" / "ci-triage.json"


def test_ci_triage_job_creates_exactly_one_issue(
    api_url: str, fake_provider_url: str, fake_tracker_url: str, stack_database_url: str
) -> None:
    """The workload with a visible side effect: one job, one issue in the tracker.

    The tracker's own list is the evidence, not the step log: it holds what
    was really created outside the runtime.
    """
    submitted = runtime(api_url, "submit", str(CI_TRIAGE), "--follow")
    assert submitted.returncode == 0, submitted.stderr
    job_id, *step_lines, outcome = submitted.stdout.splitlines()
    assert outcome.startswith("succeeded")
    assert [line.split()[1] for line in step_lines] == [
        "model_request",
        "model_response",
        "tool_intent",
        "tool_result",
        "model_request",
        "model_response",
        "tool_intent",
        "tool_result",
        "model_request",
        "model_response",
    ]

    # The tracker: exactly one issue carries an operation id of this job.
    issues = httpx2.get(f"{fake_tracker_url}/issues", timeout=10).json()
    ours = [issue for issue in issues if issue["operation_id"].startswith(f"{job_id}:")]
    assert len(ours) == 1
    issue = ours[0]
    assert issue["title"] == "CI failed: test_checkout_total"
    assert "assert 1999 == 2099" in issue["body"]

    # The operation id is the job id and the model's id for the call, and the
    # step log names the same call and the same issue number.
    job, steps = database_rows(stack_database_url, job_id)
    assert job["status"] == "succeeded"
    intent, result = json.loads(steps[6]["payload"]), json.loads(steps[7]["payload"])
    assert intent["name"] == "create_issue"
    assert issue["operation_id"] == f"{job_id}:{intent['tool_use_id']}"
    assert result == {
        "tool_use_id": intent["tool_use_id"],
        "content": f"created issue {issue['id']}: CI failed: test_checkout_total",
        "is_error": False,
    }

    # The provider was told the issue number in the third request.
    log = httpx2.get(
        f"{fake_provider_url}/_fake/requests", params={"user_id": job_id}, timeout=10
    ).json()
    assert [(entry["status"], entry["turn"]) for entry in log] == [(200, 0), (200, 1), (200, 2)]
    assert f"created issue {issue['id']}" in json.dumps(log[2]["body"]["messages"])
