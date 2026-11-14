"""Worker entry point: `python -m runtime.worker`."""

import asyncio
import logging
import signal
from datetime import timedelta

from runtime.config import WorkerConfig
from runtime.failpoints import Failpoints
from runtime.logs import configure_logging
from runtime.provider.messages import AnthropicProvider
from runtime.sandbox.docker import Docker
from runtime.sandbox.runner import Sandbox, check_host
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
    if config.sandbox is not None:
        # Before anything starts: on a host where a command could stop its
        # own timeout, the worker does not run at all.
        check_host()
    engine = create_engine(config.database_url)
    provider = AnthropicProvider(api_key=config.provider.api_key, base_url=config.provider.base_url)
    settings = AgentSettings(
        default_model=config.provider.model,
        max_tokens=config.provider.max_tokens,
        workspace_root=config.workspace_root,
        lease=timedelta(seconds=config.lease_seconds),
        heartbeat=config.heartbeat_seconds,
        failpoints=Failpoints(config.failpoint, tool=config.failpoint_tool),
    )
    docker: Docker | None = None
    sandbox: Sandbox | None = None
    if config.sandbox is not None:
        # One Docker client for the worker's life, shared by every sandboxed call.
        docker = Docker(config.sandbox.docker_socket)
        sandbox = Sandbox(docker, config.sandbox.settings)
    if config.failpoint or config.chaos_log_dir:
        log.warning(
            "chaos settings are on; this worker is for failure tests",
            extra={"failpoint": config.failpoint, "chaos_tools": bool(config.chaos_log_dir)},
        )
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(keep_alive(config.alive_file, stop, ALIVE_INTERVAL_SECONDS))
            tasks.create_task(
                work(
                    engine,
                    provider,
                    default_tools(
                        tracker_url=config.tracker_url,
                        chaos_log_dir=config.chaos_log_dir,
                        sandbox=sandbox,
                    ),
                    settings,
                    worker_id=config.worker_id,
                    poll_interval=config.poll_interval,
                    stop=stop,
                )
            )
    finally:
        if docker is not None:
            await docker.aclose()
        await provider.aclose()
        await engine.dispose()


def main() -> None:
    configure_logging()
    asyncio.run(run(WorkerConfig.from_env()))


if __name__ == "__main__":
    main()
