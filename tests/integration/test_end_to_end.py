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
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import asyncpg
import httpx2
import pytest

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
    assert [tool["name"] for tool in log[0]["body"]["tools"]] == [
        "read_file",
        "list_dir",
        "run_shell",
        "create_issue",
    ]
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


SHELL = REPO_ROOT / "examples" / "shell.json"


def test_shell_job_runs_its_commands_in_the_sandbox(
    api_url: str, fake_provider_url: str, stack_database_url: str
) -> None:
    """The stack's worker starts tool containers through the Docker socket.

    The listing shows files owned by the worker's uid 10001, which the
    sandbox image names `sandbox`. The command reads
    the file the spec wrote and writes one next to it in the same directory.
    """
    submitted = runtime(api_url, "submit", str(SHELL), "--follow")
    assert submitted.returncode == 0, submitted.stderr
    job_id, *_, outcome = submitted.stdout.splitlines()
    assert outcome.startswith("succeeded")

    _, steps = database_rows(stack_database_url, job_id)
    results = [json.loads(row["payload"]) for row in steps if row["kind"] == "tool_result"]
    listing, shell = results
    assert listing["is_error"] is False
    assert "-rw-r--r-- 1 sandbox" in listing["content"]
    assert "hello.txt" in listing["content"]
    assert shell == {
        "tool_use_id": shell["tool_use_id"],
        "content": "Hello from the workspace.\nhello.txt\nnote.txt\n[exit status 0]",
        "is_error": False,
    }

    # What the provider was sent: the same results.
    log = httpx2.get(
        f"{fake_provider_url}/_fake/requests", params={"user_id": job_id}, timeout=10
    ).json()
    sent = log[2]["body"]["messages"][4]["content"][0]
    assert sent["content"] == shell["content"]


APPROVAL = REPO_ROOT / "examples" / "approval.json"
FINAL = ("succeeded", "failed", "cancelled")


def wait_for[T](read: Callable[[], T | None], what: str) -> T:
    """`read()` once it is not None, within TIMEOUT_SECONDS."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        found = read()
        if found is not None:
            return found
        time.sleep(0.2)
    raise AssertionError(f"no {what} within {TIMEOUT_SECONDS} s")


@pytest.mark.parametrize("answer", ["approve", "deny"])
def test_each_question_reaches_the_receiver_once_before_it_is_answered(
    api_url: str, fake_receiver_url: str, stack_database_url: str, answer: str
) -> None:
    """ADR 0029 in the stack: the worker parks, a worker posts, the person answers.

    The fake receiver's log is the evidence that the question left the
    runtime. A run with no failure posts each question once.
    """
    submitted = runtime(api_url, "submit", str(APPROVAL))
    assert submitted.returncode == 0, submitted.stderr
    job_id = submitted.stdout.split()[0]

    def parked_or_final() -> tuple[str, int | None] | None:
        job, steps = database_rows(stack_database_url, job_id)
        if job["status"] in FINAL:
            return job["status"], None
        if job["status"] != "needs_approval":
            return None
        asked = [row["seq"] for row in steps if row["kind"] == "approval_requested"]
        return job["status"], asked[-1]

    def arrived(question: int) -> list[Any] | None:
        log = httpx2.get(f"{fake_receiver_url}/notifications", timeout=10).json()
        ours = [a for a in log if a["body"] and a["body"]["id"] == f"{job_id}:{question}"]
        return ours or None

    def logged(question: int) -> bool | None:
        _, steps = database_rows(stack_database_url, job_id)
        sent = [
            row
            for row in steps
            if row["kind"] == "notification_sent"
            and json.loads(row["payload"]) == {"question": question}
        ]
        return True if sent else None

    def moved_on(question: int) -> bool | None:
        return None if parked_or_final() == ("needs_approval", question) else True

    questions = []
    while True:
        status, question = wait_for(parked_or_final, "park or end")
        if question is None:
            break
        questions.append(question)
        [arrival] = wait_for(partial(arrived, question), "notification")
        assert arrival["status"] == 200
        assert arrival["body"] == {
            "id": f"{job_id}:{question}",
            "job_id": job_id,
            "question": question,
            "tool": "run_shell",
            "reason": "before_run",
        }
        wait_for(partial(logged, question), "notification_sent step")
        answered = runtime(api_url, answer, job_id, str(question))
        assert answered.returncode == 0, answered.stderr
        wait_for(partial(moved_on, question), "move on from the answered question")

    assert status == "succeeded"
    assert questions
    # Still once each, after the job ended: no late repeat.
    log = httpx2.get(f"{fake_receiver_url}/notifications", timeout=10).json()
    ids = [a["body"]["id"] for a in log if a["body"] and a["body"]["job_id"] == job_id]
    assert ids == [f"{job_id}:{question}" for question in questions]
    # Each `notification_sent` comes after its question and before its answer.
    _, steps = database_rows(stack_database_url, job_id)
    kinds = [row["kind"] for row in steps]
    for question in questions:
        at = next(i for i, row in enumerate(steps) if row["seq"] == question)
        assert kinds[at + 1 : at + 4] == [
            "status_changed",
            "notification_sent",
            f"approval_{'given' if answer == 'approve' else 'denied'}",
        ]
