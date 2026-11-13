"""read_file: read one text file from the job's workspace."""

from pathlib import Path
from typing import Any

from runtime.tools.base import Tool, ToolResult
from runtime.tools.paths import PathNotAllowed, resolve_in_workspace

# A larger file is refused, not cut: the model is told the size and can
# decide what to do.
MAX_BYTES = 64 * 1024


def _read_file(workspace: Path, tool_input: dict[str, Any]) -> ToolResult:
    path = tool_input.get("path")
    if not isinstance(path, str):
        return ToolResult("input must have a string `path`", is_error=True)
    try:
        target = resolve_in_workspace(workspace, path)
    except PathNotAllowed as error:
        return ToolResult(f"path not allowed: {error}", is_error=True)
    if not target.is_file():
        return ToolResult(f"no such file: {path}", is_error=True)
    with target.open("rb") as file:
        # One byte more than the limit tells a file at the limit from one over it.
        data = file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        size = target.stat().st_size
        return ToolResult(f"{path} is {size} bytes; the limit is {MAX_BYTES}", is_error=True)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ToolResult(f"{path} is not a UTF-8 text file", is_error=True)
    if "\x00" in text:
        return ToolResult(f"{path} is not a text file: it contains NUL bytes", is_error=True)
    return ToolResult(text)


READ_FILE = Tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the job workspace and return its content."
        f" Files larger than {MAX_BYTES} bytes are refused."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to the workspace, for example notes/todo.txt",
            }
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    run=_read_file,
)
