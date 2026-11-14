"""The container a tool call runs in: built from fixed settings, the command its only input."""

import uuid
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from runtime.sandbox.docker import REQUEST_TIMEOUT
from runtime.sandbox.runner import (
    EPOCH_LABEL,
    JOB_LABEL,
    LABEL,
    SandboxSettings,
    container_config,
    ends_within,
)

SETTINGS = SandboxSettings(image="sandbox:test", volume="workspaces")
JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")
EPOCH = 7
# What runs before the command: the timeout inside the container, at the
# worker's timeout plus a margin.
PREFIX = ["/usr/bin/timeout", "--signal=KILL", "--", "32"]


def test_the_container_is_locked_down() -> None:
    config = container_config(SETTINGS, JOB, EPOCH, ["ls"])
    host = config["HostConfig"]

    assert config["Image"] == "sandbox:test"
    assert config["User"] == "10001:10001"
    assert config["NetworkDisabled"] is True
    assert config["Tty"] is False
    assert config["OpenStdin"] is False
    assert host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert "CapAdd" not in host
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Privileged"] is False
    assert host["NanoCpus"] == 1_000_000_000
    # Swap equal to memory: no swap on top of the limit.
    assert host["Memory"] == host["MemorySwap"] == 256 * 1024 * 1024
    assert host["PidsLimit"] == 64
    # Output reaches the worker through the attach stream only. No log
    # driver keeps a copy on the host.
    assert host["LogConfig"] == {"Type": "none", "Config": {}}
    assert host["Tmpfs"] == {"/tmp": "rw,noexec,nosuid,nodev,size=16m"}  # noqa: S108
    assert "Binds" not in host
    assert "Devices" not in host


def test_the_one_mount_is_the_jobs_own_directory_of_the_volume() -> None:
    config = container_config(SETTINGS, JOB, EPOCH, ["ls"])

    assert config["HostConfig"]["Mounts"] == [
        {
            "Type": "volume",
            "Source": "workspaces",
            "Target": "/workspace",
            "ReadOnly": False,
            "VolumeOptions": {"NoCopy": True, "Subpath": str(JOB)},
        }
    ]
    assert config["WorkingDir"] == "/workspace"


def test_the_container_names_its_job_and_lease_epoch() -> None:
    config = container_config(SETTINGS, JOB, EPOCH, ["ls"])

    assert config["Labels"] == {LABEL: "1", JOB_LABEL: str(JOB), EPOCH_LABEL: "7"}


def test_the_command_runs_under_a_timeout_inside_the_container() -> None:
    """So that it ends when the worker that started it is gone."""
    short = SandboxSettings(image="sandbox:test", volume="workspaces", timeout=2.5)

    config = container_config(short, JOB, EPOCH, ["-x", "y"])

    # `--` before the duration: a command that starts with a dash is still the command.
    assert config["Cmd"] == ["/usr/bin/timeout", "--signal=KILL", "--", "4.5", "-x", "y"]


def test_a_container_ends_within_the_start_bound_plus_its_timeout() -> None:
    settings = SandboxSettings(
        image="sandbox:test", volume="workspaces", timeout=30, start_within=15
    )

    # Start no later than 15 s, the start request takes up to its own
    # timeout, then the command's timeout inside the container.
    assert ends_within(settings) == 15 + REQUEST_TIMEOUT + 30 + 2


def without_command(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key != "Cmd"}


@given(
    first=st.lists(st.text(), min_size=1, max_size=5),
    second=st.lists(st.text(), min_size=1, max_size=5),
)
def test_the_command_reaches_only_the_command(first: list[str], second: list[str]) -> None:
    one = container_config(SETTINGS, JOB, EPOCH, first)
    two = container_config(SETTINGS, JOB, EPOCH, second)

    assert one["Cmd"] == PREFIX + first
    assert two["Cmd"] == PREFIX + second
    assert without_command(one) == without_command(two)


@pytest.mark.parametrize(
    "user", ["0:0", "0:10001", "10001:0", "00:5", "root", "10001", "", "1:2:3", "-1:5"]
)
def test_a_user_that_is_root_or_not_numeric_is_refused(user: str) -> None:
    with pytest.raises(ValueError, match="user"):
        SandboxSettings(image="sandbox:test", volume="workspaces", user=user)


@pytest.mark.parametrize(
    "field",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": float("inf")},
        {"start_within": 0},
        {"start_within": float("nan")},
        {"start_within": float("inf")},
        {"output_limit": 0},
        {"cpus": 0},
        {"cpus": 0.001},
        {"cpus": float("nan")},
        {"memory": 0},
        {"pids": 0},
        {"tmp_size": 0},
        {"tmp_size": 1024 * 1024 - 1},
        {"image": ""},
        {"volume": ""},
    ],
)
def test_a_limit_that_is_not_positive_is_refused(field: dict[str, Any]) -> None:
    values: dict[str, Any] = {"image": "sandbox:test", "volume": "workspaces"} | field
    with pytest.raises(ValueError, match=next(iter(field))):
        SandboxSettings(**values)
