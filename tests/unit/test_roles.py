"""The start check gives up when the service is stopped while the database cannot be reached."""

import asyncio
import os
import select
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


def refused_line(process: subprocess.Popen[str], timeout: float) -> str:
    """Read the process's stderr until a line says it is trying again. Fails after `timeout`."""
    assert process.stderr is not None
    deadline = time.monotonic() + timeout
    seen = ""
    while (left := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([process.stderr], [], [], left)
        if not ready:
            break
        line = process.stderr.readline()
        if not line:
            break
        seen += line
        if "trying again" in line:
            return seen
    raise AssertionError(f"no retry within {timeout} s: {seen!r}")


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
        # Once it says so, it has been refused and keeps trying. A fixed
        # sleep is not enough: the first start in a new environment compiles
        # the imports first, and a signal before the handler kills it.
        first = refused_line(api, timeout=30)
        assert api.poll() is None
        api.send_signal(signal.SIGTERM)
        _, rest = api.communicate(timeout=5)
    finally:
        api.kill()
        api.wait()
    assert api.returncode == 0, first + rest
    assert "trying again" in first
