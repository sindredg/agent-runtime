"""Tools the agent can call."""

from runtime.tools.base import ToolRegistry
from runtime.tools.create_issue import create_issue_tool
from runtime.tools.read_file import READ_FILE


def default_tools(*, tracker_url: str | None = None) -> ToolRegistry:
    """The tools a worker offers. `create_issue` only when it has a tracker to talk to."""
    tools = [READ_FILE]
    if tracker_url:
        tools.append(create_issue_tool(tracker_url))
    return ToolRegistry(tools)
