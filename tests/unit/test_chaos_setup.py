"""The chaos session's check that the workers can write the crash tool's log.

The log directory is a bind mount of `.chaos`. Docker creates it as root
when it is not there at the start of the stack, and the workers then cannot
write to it. The crash tool would fail instead of ending its worker, and the
crash loop test would fail for a reason that has nothing to do with what it
tests.
"""

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

CONFTEST = Path(__file__).resolve().parents[1] / "chaos" / "conftest.py"


@pytest.fixture(scope="module")
def chaos() -> ModuleType:
    """The chaos tests' conftest, loaded as a plain module. Nothing in it starts the stack."""
    spec = importlib.util.spec_from_file_location("chaos_conftest", CONFTEST)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Shell:
    """Stands in for running a command in a worker. Records what was asked."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, *args: str, **options: Any) -> str:
        self.commands.append(args)
        return self.answer


def test_the_worker_is_asked_about_the_directory_it_was_given(chaos: ModuleType) -> None:
    shell = Shell("writable\n")
    assert chaos.worker_can_write("worker-1", run=shell)
    assert shell.commands[0][:3] == ("docker", "exec", "worker-1")
    assert "CHAOS_LOG_DIR" in shell.commands[0][-1]


@pytest.mark.parametrize("answer", ["", "sh: 1: test: not found\n", "permission denied\n"])
def test_anything_but_a_clear_yes_from_the_worker_is_a_no(chaos: ModuleType, answer: str) -> None:
    assert not chaos.worker_can_write("worker-1", run=Shell(answer))


def test_the_tests_can_write_a_directory_of_their_own_or_create_a_missing_one(
    chaos: ModuleType, tmp_path: Path
) -> None:
    assert chaos.host_can_write(tmp_path)
    assert chaos.host_can_write(tmp_path / "missing")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to any directory")
def test_the_tests_cannot_write_a_directory_that_is_closed_to_them(
    chaos: ModuleType, tmp_path: Path
) -> None:
    closed = tmp_path / "closed"
    closed.mkdir(mode=0o555)
    assert not chaos.host_can_write(closed)


def test_a_directory_that_cannot_be_written_ends_the_session_and_says_what_to_do(
    chaos: ModuleType,
) -> None:
    with pytest.raises(pytest.exit.Exception) as stopped:
        chaos.stop_unless(False)
    assert stopped.value.returncode == 1
    message = str(stopped.value)
    assert ".chaos" in message
    assert "make chaos" in message


def test_a_writable_directory_lets_the_session_go_on(chaos: ModuleType) -> None:
    chaos.stop_unless(True)
