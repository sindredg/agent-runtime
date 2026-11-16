"""read_file: read one text file from the job's workspace."""

import os
import stat
from pathlib import Path

from runtime.tools.base import Tool, ToolCall, ToolResult
from runtime.tools.paths import PathNotAllowed, resolve_in_workspace

# A larger file is refused, not cut: the model is told the size and can
# decide what to do.
MAX_BYTES = 64 * 1024


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    left = limit
    while left > 0 and (chunk := os.read(fd, left)):
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _read_file(call: ToolCall) -> ToolResult:
    workspace = call.workspace
    path = call.input.get("path")
    if not isinstance(path, str):
        return ToolResult("input must have a string `path`", is_error=True)
    try:
        target = resolve_in_workspace(workspace, path)
    except PathNotAllowed as error:
        return ToolResult(f"path not allowed: {error}", is_error=True)
    try:
        # O_PATH opens nothing for reading: a device or FIFO swapped in for
        # the file has no effect, and the open does not block.
        fd = os.open(target, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return ToolResult(f"no such file: {path}", is_error=True)
    try:
        # A command that still runs can swap a link in after the check above.
        # What was opened is checked, not the name.
        opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        if not opened.is_relative_to(workspace.resolve(strict=True)):
            return ToolResult(f"path not allowed: {path!r} leads out of the workspace", True)
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            return ToolResult(f"no such file: {path}", is_error=True)
        # The same file, now for reading: /proc/self/fd/N is the opened file,
        # not a name that can be swapped again.
        readable = os.open(
            f"/proc/self/fd/{fd}", os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC
        )
    finally:
        os.close(fd)
    try:
        # One byte more than the limit tells a file at the limit from one over it.
        data = _read_up_to(readable, MAX_BYTES + 1)
    finally:
        os.close(readable)
    if len(data) > MAX_BYTES:
        return ToolResult(
            f"{path} is {status.st_size} bytes; the limit is {MAX_BYTES}", is_error=True
        )
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
    # Reading changes nothing, so an interrupted read is simply done again.
    idempotent=True,
)
