"""The container a tool call runs in: built from fixed settings, the command its only input."""

import uuid
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from runtime.sandbox.runner import SandboxSettings, container_config

SETTINGS = SandboxSettings(image="sandbox:test", volume="workspaces")
JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


def test_the_container_is_locked_down() -> None:
    config = container_config(SETTINGS, JOB, ["ls"])
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
    config = container_config(SETTINGS, JOB, ["ls"])

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


def without_command(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key != "Cmd"}


@given(
    first=st.lists(st.text(), min_size=1, max_size=5),
    second=st.lists(st.text(), min_size=1, max_size=5),
)
def test_the_command_reaches_only_the_command(first: list[str], second: list[str]) -> None:
    one = container_config(SETTINGS, JOB, first)
    two = container_config(SETTINGS, JOB, second)

    assert one["Cmd"] == first
    assert two["Cmd"] == second
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
        {"output_limit": 0},
        {"cpus": 0},
        {"memory": 0},
        {"pids": 0},
        {"tmp_size": 0},
        {"image": ""},
        {"volume": ""},
    ],
)
def test_a_limit_that_is_not_positive_is_refused(field: dict[str, Any]) -> None:
    values: dict[str, Any] = {"image": "sandbox:test", "volume": "workspaces"} | field
    with pytest.raises(ValueError, match=next(iter(field))):
        SandboxSettings(**values)
