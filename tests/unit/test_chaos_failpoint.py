"""What a failpoint test says when the failpoint did not stop a worker exactly once.

The first worker has to reach the failpoint. If it lost its lease before,
the job is taken over and the failpoint never fires: the test fails, and its
message has to say why, from the job and the workers' logs.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "chaos" / "conftest.py"
JOB = "01a1daf8-f804-7d2d-ad25-80445c71d2f7"


@pytest.fixture(scope="module")
def chaos() -> ModuleType:
    """The chaos tests' conftest, loaded as a plain module. Nothing in it starts the stack."""
    spec = importlib.util.spec_from_file_location("chaos_conftest", CONFTEST)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_the_failpoint_tests_run_on_a_longer_lease_than_the_kills(chaos: ModuleType) -> None:
    # Three renewals fit in it, at the 2 s heartbeat the workers get for it.
    assert chaos.FAILPOINT_LEASE == 6.0
    assert chaos.FAILPOINT_LEASE > chaos.SHORT_LEASE
