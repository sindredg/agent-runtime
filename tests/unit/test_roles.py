"""The start check gives up when the service is stopped while the database cannot be reached."""

import asyncio
import time

from runtime.store.engine import create_engine
from runtime.store.roles import wait_for_app_role

# Nothing listens on the discard port, so every connection is refused.
UNREACHABLE = "postgresql://u:p@127.0.0.1:9/none"


def test_a_stop_ends_the_wait_for_the_database() -> None:
    async def run() -> bool:
        engine = create_engine(UNREACHABLE)
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.3, stop.set)
        try:
            return await asyncio.wait_for(wait_for_app_role(engine, stop, retry_seconds=10), 5)
        finally:
            await engine.dispose()

    started = time.monotonic()
    assert asyncio.run(run()) is False
    # The stop ends the pause between two tries; it does not wait out the 10 s.
    assert time.monotonic() - started < 2
