"""API entry point: `python -m runtime.api`."""

import asyncio
import signal

import uvicorn

from runtime.api.app import create_app
from runtime.config import ApiConfig
from runtime.logs import configure_logging
from runtime.store.engine import create_engine
from runtime.store.roles import wait_for_app_role


async def database_ready(database_url: str) -> bool:
    """Wait until the database answers and the role passes the start check.

    False if the process is stopped first. This runs before uvicorn, whose
    signal handler does not end the application's startup.
    """
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Docker stops a container with SIGTERM; Ctrl+C sends SIGINT.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    engine = create_engine(database_url, application_name="runtime-api")
    try:
        return await wait_for_app_role(engine, stop)
    finally:
        await engine.dispose()


def main() -> None:
    configure_logging()
    config = ApiConfig.from_env()
    if not asyncio.run(database_ready(config.database_url)):
        return
    # log_config=None: uvicorn's loggers use the JSON handler set up above.
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_config=None)


if __name__ == "__main__":
    main()
