"""Tools the agent can call."""

from runtime.tools.base import ToolRegistry


def default_tools() -> ToolRegistry:
    """The tools every worker offers."""
    return ToolRegistry()
