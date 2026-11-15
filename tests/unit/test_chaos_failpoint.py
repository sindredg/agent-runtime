"""What a failpoint test says when the failpoint did not stop a worker exactly once.

The first worker has to reach the failpoint. If it lost its lease before,
the job is taken over and the failpoint never fires: the test fails, and its
message has to say why, from the job and the workers' logs.
"""

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

JOB = "01a1daf8-f804-7d2d-ad25-80445c71d2f7"


def line(message: str, job_id: str = JOB, **fields: str) -> str:
    return json.dumps(
        {"time": "16:51:54", "level": "error", "message": message, "job_id": job_id, **fields}
    )


LOGS = "\n".join(
    [
        "a line that is not JSON",
        line("claimed job"),
        line(
            "lease renewal failed",
            exception="Traceback (most recent call last):\n  ...\nTimeoutError",
        ),
        line("lease renewal failed", job_id="another job"),
        line("job dropped: the lease is gone"),
    ]
)


def test_only_this_jobs_lease_lines_are_kept_with_the_error_type(chaos: ModuleType) -> None:
    assert chaos.lease_trouble(LOGS, JOB) == [
        "16:51:54 lease renewal failed TimeoutError",
        "16:51:54 job dropped: the lease is gone",
    ]


def test_the_message_names_the_takeovers_and_the_lease_lines(chaos: ModuleType) -> None:
    message = chaos.failpoint_missed(0, {"id": JOB, "takeovers": 1}, LOGS)
    assert "stopped a worker 0 times" in message
    assert "1 takeovers" in message
    assert "lease renewal failed TimeoutError; 16:51:54 job dropped" in message


def test_the_message_says_when_no_lease_was_lost(chaos: ModuleType) -> None:
    message = chaos.failpoint_missed(0, {"id": JOB, "takeovers": 0}, line("claimed job"))
    assert message.endswith("Lease lines in the workers' logs: none")


def workers_get(
    chaos: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **settings: Any
) -> dict[str, str]:
    """The settings `workers_up` hands to compose for the workers. Nothing is started."""
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def docker(*args: str, env: dict[str, str] | None = None, **_: Any) -> str:
        calls.append((args, env))
        return ""

    monkeypatch.setattr(chaos, "docker", docker)
    monkeypatch.setattr(chaos, "CHAOS_DIR", tmp_path)
    stack = chaos.Chaos()
    try:
        stack.workers_up(**settings)
    finally:
        for client in (stack.api, stack.provider, stack.tracker):
            client.close()
    [env] = [env for args, env in calls if "up" in args]
    assert env is not None
    return env


def test_the_failpoint_tests_give_the_workers_a_6_s_lease_renewed_every_2_s(
    chaos: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = workers_get(
        chaos,
        monkeypatch,
        tmp_path,
        lease=chaos.FAILPOINT_LEASE,
        failpoint="after_tool_intent",
        failpoint_tool="create_issue",
    )
    assert env == {
        "CHAOS_LEASE_SECONDS": "6",
        "CHAOS_HEARTBEAT_SECONDS": "2",
        "FAILPOINT": "after_tool_intent",
        "FAILPOINT_TOOL": "create_issue",
    }


def test_the_random_kills_keep_the_2_s_lease_renewed_every_half_second(
    chaos: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = workers_get(chaos, monkeypatch, tmp_path)
    assert (env["CHAOS_LEASE_SECONDS"], env["CHAOS_HEARTBEAT_SECONDS"]) == ("2", "0.5")
