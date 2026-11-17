"""The start check gives up when the service is stopped while the database cannot be reached."""

import asyncio
import os
import signal
import subprocess
import sys
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


def test_sigterm_stops_an_api_that_waits_for_the_database() -> None:
    """Docker stops the API with SIGTERM. Without the wait before uvicorn
    starts, the API either fails at once or ignores the signal until Docker
    kills it."""
    env = {"PATH": os.environ["PATH"], "DATABASE_URL": UNREACHABLE}
    api = subprocess.Popen(
        [sys.executable, "-m", "runtime.api"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        time.sleep(2)
        # Refused several times by now, and still trying.
        assert api.poll() is None
        api.send_signal(signal.SIGTERM)
        _, stderr = api.communicate(timeout=5)
    finally:
        api.kill()
        api.wait()
    assert api.returncode == 0, stderr
    assert "trying again" in stderr
