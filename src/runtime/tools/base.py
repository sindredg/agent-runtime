"""What a tool is: a name and schema for the model, and a function for the worker."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What goes back to the model. `is_error` marks a failed call; the job goes on."""

    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Called with the job's workspace and the input the model gave. It runs
    # in a thread, so it may block.
    run: Callable[[Path, dict[str, Any]], ToolResult]

    def definition(self) -> dict[str, Any]:
        """The tool as the Messages API wants it in `tools`."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """The tools a worker offers, by name."""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def definitions(self) -> tuple[dict[str, Any], ...]:
        return tuple(tool.definition() for tool in self._tools.values())
