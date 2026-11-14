"""list_dir: list a directory of the job's workspace, in a container of its own."""

from typing import Any

from runtime.tools.base import ToolResult
from runtime.tools.paths import PathNotAllowed, check_relative
from runtime.tools.sandboxed import Runner, SandboxTool


def _command(tool_input: dict[str, Any]) -> list[str] | ToolResult:
    path = tool_input.get("path", ".")
    if not isinstance(path, str):
        return ToolResult("`path` must be a string", is_error=True)
    if path != ".":
        try:
            check_relative(path)
        except PathNotAllowed as error:
            return ToolResult(f"path not allowed: {error}", is_error=True)
    # After `--`, a path that starts with a dash is still a path.
    return ["ls", "-la", "--", path]


def list_dir_tool(sandbox: Runner) -> SandboxTool:
    return SandboxTool(
        name="list_dir",
        description=(
            "List a directory of the job workspace with `ls -la`: names, sizes, owners and"
            " modes. Without a path it lists the top of the workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the workspace, for example notes",
                }
            },
            "additionalProperties": False,
        },
        command=_command,
        sandbox=sandbox,
        # Listing changes nothing, so an interrupted call is simply run again.
        idempotent=True,
    )
