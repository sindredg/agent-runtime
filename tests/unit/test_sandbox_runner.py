"""The runner against a stand-in for Docker: what a takeover removes, when a start is given up."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from runtime.sandbox import runner
from runtime.sandbox.docker import DockerError, SandboxError
from runtime.sandbox.runner import (
    EPOCH_LABEL,
    JOB_LABEL,
    LABEL,
    NotStarted,
    Sandbox,
    SandboxSettings,
)

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")


class FakeDocker:
    """Answers the calls the runner makes and records them."""

    def __init__(
        self,
        found: list[dict[str, Any]] | None = None,
        create_takes: float = 0,
        start_takes: float = 0,
        refuse_removal: frozenset[str] = frozenset(),
    ) -> None:
        self.found = found or []
        self.create_takes = create_takes
        self.start_takes = start_takes
        self.refuse_removal = refuse_removal
        self.calls: list[tuple[str, Any]] = []

    async def containers(self, labels: dict[str, str]) -> list[dict[str, Any]]:
        self.calls.append(("containers", labels))
        return self.found

    async def create(self, name: str, config: dict[str, Any]) -> None:
        self.calls.append(("create", name))
        await asyncio.sleep(self.create_takes)

    @asynccontextmanager
    async def attach(self, name: str) -> AsyncIterator[AsyncIterator[bytes]]:
        self.calls.append(("attach", name))

        async def nothing() -> AsyncIterator[bytes]:
            return
            yield b""

        yield nothing()

    async def start(self, name: str) -> None:
        self.calls.append(("start", name))
        await asyncio.sleep(self.start_takes)

    async def wait(self, name: str) -> int:
        self.calls.append(("wait", name))
        return 0

    async def remove(self, name: str) -> None:
        self.calls.append(("remove", name))
        if name in self.refuse_removal:
            raise DockerError(409, "removal of container is already in progress")


def settings(**changes: Any) -> SandboxSettings:
    return SandboxSettings(image="sandbox:test", volume="workspaces", **changes)


def container(id_: str, epoch: str | None) -> dict[str, Any]:
    labels = {JOB_LABEL: str(JOB)} | ({EPOCH_LABEL: epoch} if epoch is not None else {})
    return {"Id": id_, "Labels": labels}


def test_takeover_removes_the_jobs_containers_of_earlier_epochs_only() -> None:
    docker = FakeDocker(
        [
            container("older", "3"),
            container("much-older", "1"),
            container("own", "4"),
            container("newer", "5"),
            container("no-epoch", None),
            container("bad-epoch", "x"),
        ]
    )
    sandbox = Sandbox(docker, settings())  # type: ignore[arg-type]

    removed = asyncio.run(sandbox.remove_older(JOB, 4))

    assert removed == 4
    # Only containers the sandbox started.
    assert docker.calls[0] == ("containers", {LABEL: "1", JOB_LABEL: str(JOB)})
    assert [name for call, name in docker.calls if call == "remove"] == [
        "older",
        "much-older",
        "no-epoch",
        "bad-epoch",
    ]


def test_docker_is_not_asked_to_start_once_the_start_bound_has_passed() -> None:
    """A slow set-up must not start a command the takeover bound no longer covers."""
    docker = FakeDocker(create_takes=0.3)
    sandbox = Sandbox(docker, settings(start_within=0.1))  # type: ignore[arg-type]

    with pytest.raises(NotStarted, match=r"took more than 0\.1 s"):
        asyncio.run(sandbox.run(JOB, 1, ["true"]))

    calls = [call for call, _ in docker.calls]
    assert "start" not in calls
    # Created, so removed again.
    assert calls[-1] == "remove"


def test_a_quick_set_up_starts_the_command() -> None:
    docker = FakeDocker()
    sandbox = Sandbox(docker, settings(start_within=5))  # type: ignore[arg-type]

    run = asyncio.run(sandbox.run(JOB, 1, ["true"]))

    assert run.exit_code == 0
    assert [call for call, _ in docker.calls] == ["create", "attach", "start", "wait", "remove"]


def test_one_container_that_cannot_be_removed_does_not_stop_the_others() -> None:
    docker = FakeDocker(
        [container("first", "1"), container("second", "1")], refuse_removal=frozenset({"first"})
    )
    sandbox = Sandbox(docker, settings())  # type: ignore[arg-type]

    with pytest.raises(SandboxError, match=r"1 container\(s\) not removed: first"):
        asyncio.run(sandbox.remove_older(JOB, 2))

    assert [name for call, name in docker.calls if call == "remove"] == ["first", "second"]


def test_a_start_request_is_bounded_as_a_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not per phase: the bound of a takeover counts on it."""
    monkeypatch.setattr(runner, "REQUEST_TIMEOUT", 0.1)
    docker = FakeDocker(start_takes=5)
    sandbox = Sandbox(docker, settings())  # type: ignore[arg-type]

    with pytest.raises(SandboxError, match="did not answer the start") as raised:
        asyncio.run(sandbox.run(JOB, 1, ["true"]))

    # Asked to start, so it may have run.
    assert not isinstance(raised.value, NotStarted)
    assert [call for call, _ in docker.calls][-1] == "remove"
