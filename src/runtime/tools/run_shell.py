"""run_shell: run a shell command in the job's workspace, in a container of its own."""

from typing import Any

from runtime.tools.base import ToolResult
from runtime.tools.sandboxed import Runner, SandboxTool


def _command(tool_input: dict[str, Any]) -> list[str] | ToolResult:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return ToolResult("input must have a non-empty string `command`", is_error=True)
    if "\x00" in command:
        return ToolResult("the command contains a NUL character", is_error=True)
    # The model's text is one argument to sh. It is never split into
    # options for the container.
    return ["/bin/sh", "-c", command]


def run_shell_tool(sandbox: Runner) -> SandboxTool:
    settings = sandbox.settings
    return SandboxTool(
        name="run_shell",
        description=(
            "Run a command with /bin/sh in the job workspace and return its output and exit"
            f" status. It has no network. It is stopped after {settings.timeout:g} seconds,"
            f" and output past {settings.output_limit} bytes is cut. Only files in the"
            " workspace are kept between calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "For example: ls -la | head"}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        command=_command,
        sandbox=sandbox,
        # A command can change anything in the workspace. An interrupted one
        # is not run again without a person.
        idempotent=False,
    )
