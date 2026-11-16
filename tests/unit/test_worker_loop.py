"""The worker's outer loop: it keeps going through failures and stops when told."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from runtime.store.engine import create_engine
from runtime.tools.base import ToolRegistry
from runtime.worker import loop
from runtime.worker.agent import AgentSettings
from runtime.worker.loop import keep_alive, work


def test_keep_alive_touches_the_file_then_stops(tmp_path: Path) -> None:
    alive = tmp_path / "worker.alive"

    async def scenario() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        # The timeout turns a loop that ignores `stop` into a failure, not a hang.
        async with asyncio.timeout(2):
            await keep_alive(alive, stop, interval=0.01)

    asyncio.run(scenario())
    assert alive.exists()


def test_keep_alive_returns_at_once_when_already_stopped(tmp_path: Path) -> None:
    alive = tmp_path / "worker.alive"

    async def scenario() -> None:
        stop = asyncio.Event()
        stop.set()
        async with asyncio.timeout(2):
            await keep_alive(alive, stop, interval=60)

    asyncio.run(scenario())
    assert not alive.exists()


SETTINGS = AgentSettings(
    default_model="m",
    max_tokens=10,
    workspace_root=Path("unused"),
    lease=timedelta(seconds=7),
    heartbeat=1.0,
)


def run_work(stop_after: float | None = None, until: Callable[[], bool] | None = None) -> None:
    """Run the work loop against a database that is not there, then stop it.

    It is stopped after `stop_after` seconds, or once `until` is true.
    """

    async def watch(stop: asyncio.Event) -> None:
        assert until is not None
        # The condition is read from log records: there is no event to wait on.
        while not until():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        stop.set()

    async def scenario() -> None:
        # Port 9 is closed: a claim that reaches the network fails to connect.
        engine = create_engine("postgresql://u:p@127.0.0.1:9/none")
        stop = asyncio.Event()
        if stop_after is not None:
            asyncio.get_running_loop().call_later(stop_after, stop.set)
        watcher = asyncio.create_task(watch(stop)) if until is not None else None
        try:
            async with asyncio.timeout(10):
                await work(
                    engine,
                    provider=None,  # type: ignore[arg-type]
                    tools=ToolRegistry(),
                    settings=SETTINGS,
                    worker_id="w",
                    poll_interval=0.01,
                    stop=stop,
                )
        finally:
            if watcher is not None:
                watcher.cancel()
            await engine.dispose()

    asyncio.run(scenario())


def test_a_claim_that_fails_is_logged_and_tried_again(caplog: pytest.LogCaptureFixture) -> None:
    def failures() -> int:
        return sum(r.getMessage() == "claim failed" for r in caplog.records)

    # Stopped at the second failure, not after a fixed time: the first claim
    # also sets up the connection pool, which is slow on a loaded machine.
    run_work(until=lambda: failures() >= 2)
    # More than one: the loop went on after the first failure, and it ended when told.
    assert failures() >= 2


def test_a_job_that_cannot_be_finished_does_not_stop_the_loop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    claimed: list[str] = []

    class FakeJob:
        lease_epoch = 1
        takeovers = 0

        def __init__(self, job_id: str) -> None:
            self.id = job_id

    class NoConnection:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *error: object) -> None:
            return None

    async def claim(
        conn: object, *, worker_id: str, lease: timedelta, failed: list[Any]
    ) -> FakeJob | None:
        # The configured lease reaches the claim.
        assert lease == timedelta(seconds=7)
        if len(claimed) == 3:
            return None
        claimed.append(f"job-{len(claimed)}")
        return FakeJob(claimed[-1])

    async def crash(*args: Any, **named: Any) -> None:
        # The loop says when it sent the claim, on the monotonic clock.
        assert isinstance(named["claimed_at"], float)
        raise RuntimeError("database went away")

    monkeypatch.setattr(loop, "claim_job", claim)
    monkeypatch.setattr(loop, "run_job", crash)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncEngine.begin", lambda self: NoConnection())
    run_work(stop_after=0.2)

    assert claimed == ["job-0", "job-1", "job-2"]
    stuck = [r for r in caplog.records if r.getMessage() == "job could not be finished"]
    assert [r.__dict__["job_id"] for r in stuck] == claimed


def test_a_cleanup_that_fails_after_the_claim_does_not_lose_the_claimed_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The claim is committed by then. The job it returned still has to be run."""
    ran: list[str] = []

    class FakeJob:
        lease_epoch = 1
        takeovers = 0

        def __init__(self, job_id: str) -> None:
            self.id = job_id

    class NoConnection:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *error: object) -> None:
            return None

    async def claim(
        conn: object, *, worker_id: str, lease: timedelta, failed: list[Any]
    ) -> FakeJob | None:
        if ran:
            return None
        failed.append(FakeJob("capped"))
        return FakeJob("claimed")

    async def run(
        engine: object, provider: object, tools: object, job: Any, *a: Any, **k: Any
    ) -> None:
        ran.append(job.id)

    def broken_cleanup(root: Path, job_id: str) -> None:
        raise OSError("volume gone")

    monkeypatch.setattr(loop, "claim_job", claim)
    monkeypatch.setattr(loop, "run_job", run)
    monkeypatch.setattr(loop, "remove_workspace", broken_cleanup)
    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncEngine.begin", lambda self: NoConnection())
    run_work(stop_after=0.2)

    assert ran == ["claimed"]
    assert any("workspace" in r.getMessage() for r in caplog.records)
