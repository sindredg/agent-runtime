"""Tools the agent can call."""

from pathlib import Path

from runtime.tools.base import ToolRegistry
from runtime.tools.crash_worker import crash_worker_tool
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.read_file import READ_FILE


def default_tools(
    *, tracker_url: str | None = None, chaos_log_dir: Path | None = None
) -> ToolRegistry:
    """The tools a worker offers.

    `create_issue` only when the worker has a tracker to talk to.
    `crash_worker` only when chaos tools are switched on, which gives
    `chaos_log_dir`.
    """
    tools = [READ_FILE]
    if tracker_url:
        tools.append(create_issue_tool(tracker_url))
    if chaos_log_dir is not None:
        tools.append(crash_worker_tool(chaos_log_dir))
    return ToolRegistry(tools)
