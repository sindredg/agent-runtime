"""Worker entry point: `python -m runtime.worker`."""

import asyncio
import logging
import signal
from datetime import timedelta

from runtime.config import WorkerConfig
from runtime.logs import configure_logging
from runtime.provider.messages import AnthropicProvider
from runtime.store.engine import create_engine
from runtime.tools import default_tools
from runtime.worker.agent import AgentSettings
from runtime.worker.loop import keep_alive, work

ALIVE_INTERVAL_SECONDS = 5.0

log = logging.getLogger("runtime.worker")


async def run(config: WorkerConfig) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Docker stops a container with SIGTERM; Ctrl+C sends SIGINT.
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    log.info(
        "worker started",
        extra={"worker_id": config.worker_id, "default_model": config.provider.model},
    )
    engine = create_engine(config.database_url)
    provider = AnthropicProvider(api_key=config.provider.api_key, base_url=config.provider.base_url)
    settings = AgentSettings(
        default_model=config.provider.model,
        max_tokens=config.provider.max_tokens,
        workspace_root=config.workspace_root,
        lease=timedelta(seconds=config.lease_seconds),
        heartbeat=config.heartbeat_seconds,
    )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(keep_alive(config.alive_file, stop, ALIVE_INTERVAL_SECONDS))
            tasks.create_task(
                work(
                    engine,
                    provider,
                    default_tools(),
                    settings,
                    worker_id=config.worker_id,
                    poll_interval=config.poll_interval,
                    stop=stop,
                )
            )
    finally:
        await provider.aclose()
        await engine.dispose()


def main() -> None:
    configure_logging()
    asyncio.run(run(WorkerConfig.from_env()))


if __name__ == "__main__":
    main()
