"""Tools the agent can call."""

from runtime.tools.base import ToolRegistry
from runtime.tools.read_file import READ_FILE


def default_tools() -> ToolRegistry:
    """The tools every worker offers."""
    return ToolRegistry([READ_FILE])
