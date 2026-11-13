"""Worker entry point: `python -m runtime.worker`.

The worker does no work yet. It checks Postgres once, then idles and refreshes
a liveness file that the container health check reads.
"""

import asyncio
import os
import signal
from pathlib import Path

from runtime.store.ping import ping

ALIVE_INTERVAL_SECONDS = 5.0


async def idle(alive_path: Path, stop: asyncio.Event, interval: float) -> None:
    """Touch `alive_path` every `interval` seconds until `stop` is set."""
    while not stop.is_set():
        # Blocking, but it is one metadata write to a tmpfs file every few seconds.
        alive_path.touch()  # noqa: ASYNC240
        try:
            # Wakes early when stop is set, so shutdown does not wait a full interval.
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue


async def run(dsn: str, alive_path: Path) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Docker stops a container with SIGTERM; Ctrl+C sends SIGINT.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await ping(dsn)
    await idle(alive_path, stop, ALIVE_INTERVAL_SECONDS)


def main() -> None:
    alive_path = Path(os.environ.get("WORKER_ALIVE_FILE", "/tmp/worker.alive"))  # noqa: S108
    asyncio.run(run(os.environ["DATABASE_URL"], alive_path))


if __name__ == "__main__":
    main()
