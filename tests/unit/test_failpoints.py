"""Failpoints: named places where a worker stops dead, for tests only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from runtime.config import ConfigError, WorkerConfig
from runtime.failpoints import NAMES, Failpoints
from runtime.tools import default_tools
from runtime.tools.base import ToolCall
from runtime.tools.crash_worker import crash_worker_tool

WORKER_ENV = {"ANTHROPIC_API_KEY": "k", "MODEL": "m", "DATABASE_URL": "postgresql://u:p@h/db"}


class Crashed(BaseException):
    """Stands in for the process ending."""


def crash() -> None:
    raise Crashed


def test_the_six_failpoints_are_named() -> None:
    assert NAMES == (
        "after_claim",
        "after_model_request",
        "mid_stream",
        "after_tool_intent",
        "after_tool_ran",
        "after_tool_result",
    )


@pytest.mark.parametrize("name", NAMES)
def test_a_failpoint_is_inert_unless_it_is_the_one_switched_on(name: str) -> None:
    Failpoints(crash=crash).hit(name, takeovers=0)
    other = next(n for n in NAMES if n != name)
    Failpoints(other, crash=crash).hit(name, takeovers=0)


@pytest.mark.parametrize("name", NAMES)
def test_the_failpoint_that_is_switched_on_fires(name: str) -> None:
    with pytest.raises(Crashed):
        Failpoints(name, crash=crash).hit(name, takeovers=0)


@pytest.mark.parametrize("takeovers", [1, 2, 5])
def test_a_failpoint_fires_only_on_a_job_that_was_never_taken_over(takeovers: int) -> None:
    """Otherwise the worker that takes the job over would stop at the same place, every time."""
    Failpoints("after_claim", crash=crash).hit("after_claim", takeovers=takeovers)


def test_a_tool_failpoint_can_be_limited_to_one_tool() -> None:
    points = Failpoints("after_tool_ran", tool="create_issue", crash=crash)
    points.hit("after_tool_ran", takeovers=0, tool="read_file")
    with pytest.raises(Crashed):
        points.hit("after_tool_ran", takeovers=0, tool="create_issue")
    # Without a limit it fires at the first tool.
    with pytest.raises(Crashed):
        Failpoints("after_tool_ran", crash=crash).hit(
            "after_tool_ran", takeovers=0, tool="read_file"
        )


def test_an_unknown_failpoint_name_is_refused() -> None:
    with pytest.raises(ValueError, match="after_lunch"):
        Failpoints("after_lunch")


def test_the_real_failpoint_ends_the_process_like_a_kill() -> None:
    """No cleanup, no exit handlers: exit status 137, as after SIGKILL."""
    code = (
        "import atexit, sys\n"
        "from runtime.failpoints import Failpoints\n"
        "atexit.register(lambda: print('cleanup ran'))\n"
        "Failpoints('after_claim').hit('after_claim', takeovers=0)\n"
        "print('still alive')\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed command
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 137
    assert done.stdout == ""
    assert "failpoint" in done.stderr


# Settings


def test_no_failpoint_and_no_chaos_tools_by_default() -> None:
    config = WorkerConfig.from_env(WORKER_ENV)
    assert (config.failpoint, config.failpoint_tool, config.chaos_log_dir) == (None, None, None)


def test_failpoint_settings_are_read() -> None:
    config = WorkerConfig.from_env(
        {
            **WORKER_ENV,
            "FAILPOINT": "after_tool_ran",
            "FAILPOINT_TOOL": "create_issue",
            "CHAOS_TOOLS": "1",
            "CHAOS_LOG_DIR": "/chaos",
        }
    )
    assert (config.failpoint, config.failpoint_tool) == ("after_tool_ran", "create_issue")
    assert config.chaos_log_dir == Path("/chaos")


def test_an_unknown_failpoint_in_the_environment_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="FAILPOINT"):
        WorkerConfig.from_env({**WORKER_ENV, "FAILPOINT": "after_lunch"})


@pytest.mark.parametrize("flag", ["", "0", "true", "yes"])
def test_chaos_tools_are_on_only_for_the_value_1(flag: str) -> None:
    config = WorkerConfig.from_env({**WORKER_ENV, "CHAOS_TOOLS": flag, "CHAOS_LOG_DIR": "/chaos"})
    assert config.chaos_log_dir is None


def test_chaos_tools_need_a_log_directory() -> None:
    with pytest.raises(ConfigError, match="CHAOS_LOG_DIR"):
        WorkerConfig.from_env({**WORKER_ENV, "CHAOS_TOOLS": "1"})


# The tool that takes its worker down


def test_the_crash_tool_is_absent_unless_chaos_tools_are_on(tmp_path: Path) -> None:
    assert default_tools().get("crash_worker") is None
    assert default_tools(tracker_url="http://t").get("crash_worker") is None
    assert default_tools(chaos_log_dir=tmp_path).get("crash_worker") is not None


def test_the_crash_tool_logs_its_run_then_takes_the_worker_down(tmp_path: Path) -> None:
    tool = crash_worker_tool(tmp_path, crash=crash)
    # Idempotent on purpose: a job that keeps killing its worker must be run
    # again without a person, or it would never reach the takeover limit.
    assert tool.idempotent is True
    for _ in range(2):
        with pytest.raises(Crashed):
            tool.run(ToolCall(tmp_path, {}, "job-1:toolu_1", 1))
    lines = (tmp_path / "crash_worker.log").read_text().splitlines()
    assert [json.loads(line)["operation_id"] for line in lines] == ["job-1:toolu_1"] * 2
