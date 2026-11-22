"""Run the fake receiver in the caller's event loop, on a free port of 127.0.0.1.

For tests that need a real socket: a redirect, a wait, a proxy setting.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI


@contextlib.asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[str]:
    """Serve `app` until the block ends. Yields its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        # A request still waiting on a queued hang would hold the shutdown.
        server.force_exit = True
        await task
