"""The liveness file is kept fresh until the worker is told to stop."""

import asyncio
from pathlib import Path

from runtime.worker.loop import keep_alive


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
