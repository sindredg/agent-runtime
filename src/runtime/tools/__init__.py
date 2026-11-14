"""Tools the agent can call."""

from pathlib import Path

from runtime.tools.base import AnyTool, ToolRegistry
from runtime.tools.crash_worker import crash_worker_tool
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.list_dir import list_dir_tool
from runtime.tools.read_file import READ_FILE
from runtime.tools.run_shell import run_shell_tool
from runtime.tools.sandboxed import Runner


def default_tools(
    *,
    tracker_url: str | None = None,
    chaos_log_dir: Path | None = None,
    sandbox: Runner | None = None,
) -> ToolRegistry:
    """The tools a worker offers.

    `run_shell` and `list_dir` only when the worker has a sandbox to run
    them in. `create_issue` only when the worker has a tracker to talk to.
    `crash_worker` only when chaos tools are switched on, which gives
    `chaos_log_dir`.
    """
    tools: list[AnyTool] = [READ_FILE]
    if sandbox is not None:
        tools += [list_dir_tool(sandbox), run_shell_tool(sandbox)]
    if tracker_url:
        tools.append(create_issue_tool(tracker_url))
    if chaos_log_dir is not None:
        tools.append(crash_worker_tool(chaos_log_dir))
    return ToolRegistry(tools)
