"""The idle worker keeps its liveness file fresh and stops when asked."""

import asyncio
from pathlib import Path

from runtime.worker.__main__ import idle


def test_idle_touches_file_then_stops(tmp_path: Path) -> None:
    alive = tmp_path / "worker.alive"

    async def scenario() -> None:
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        # The timeout turns a loop that ignores `stop` into a failure, not a hang.
        async with asyncio.timeout(2):
            await idle(alive, stop, interval=0.01)

    asyncio.run(scenario())
    assert alive.exists()


def test_idle_returns_at_once_when_already_stopped(tmp_path: Path) -> None:
    alive = tmp_path / "worker.alive"

    async def scenario() -> None:
        stop = asyncio.Event()
        stop.set()
        async with asyncio.timeout(2):
            await idle(alive, stop, interval=60)

    asyncio.run(scenario())
    assert not alive.exists()
