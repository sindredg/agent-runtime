"""The job spec: what a job is asked to do. Stored in `jobs.spec`."""

from pathlib import PurePosixPath
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from runtime.scope import MAX_TARGETS, Target
from runtime.tools.paths import check_relative

# The spec is one database row. Files that seed the workspace are part of it,
# so they are bounded. The limit counts paths and contents (ADR 0032).
MAX_FILES_BYTES = 8 * 1024 * 1024
# Files plus the directories their paths make, and levels of directories.
# Both stay far under the workspace watcher's 100,000 entries and 256 levels,
# so a seed leaves the job room and the watcher can measure it (ADR 0032).
MAX_FILES_ENTRIES = 10_000
MAX_FILES_DEPTH = 32
# Limits most file systems have: one name, and a whole path.
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 1024
# Characters in `prompt` and in `system`.
MAX_TEXT_LENGTH = 1_000_000
# Tools a spec can name in `needs_approval`, and characters in one name.
MAX_GATED_TOOLS = 100
MAX_TOOL_NAME_LENGTH = 200


def _size(text: str) -> int:
    # surrogatepass: text that is not valid Unicode is still measured here.
    # The store refuses it with its own error.
    return len(text.encode("utf-8", "surrogatepass"))


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # The first user message.
    prompt: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    system: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_LENGTH)
    # Left out, the worker's configured model is used.
    model: str | None = Field(default=None, min_length=1, max_length=200)
    # Text files written into the job's workspace before it starts:
    # relative path -> content.
    files: dict[str, str] = Field(default_factory=dict)
    # What the job's tools may reach on the network (ADR 0009). Left out, they
    # reach nothing. The job cannot change it.
    scope: tuple[Target, ...] | None = Field(default=None, min_length=1, max_length=MAX_TARGETS)
    # Names of tools whose calls wait for a person's approval in this job, on
    # top of the tools that always do (ADR 0011). A list of names can only
    # add: there is no way to write that a tool needs none.
    needs_approval: (
        tuple[Annotated[StrictStr, Field(min_length=1, max_length=MAX_TOOL_NAME_LENGTH)], ...]
        | None
    ) = Field(default=None, max_length=MAX_GATED_TOOLS)

    @field_validator("files")
    @classmethod
    def _files_can_all_be_written(cls, files: dict[str, str]) -> dict[str, str]:
        """Refuse a set of files that could not all exist side by side.

        Each path has to be in its plain form, so two spellings of one path
        (`a` and `./a`) cannot both be given. No path may be a directory of
        another (`a` and `a/b`). The counts and sizes are checked first, so
        the work is bounded and no error quotes a path longer than
        MAX_PATH_BYTES.
        """
        if len(files) > MAX_FILES_ENTRIES:
            raise ValueError(f"files name more than {MAX_FILES_ENTRIES:,} paths")
        size = sum(_size(path) + _size(content) for path, content in files.items())
        if size > MAX_FILES_BYTES:
            raise ValueError(f"files hold {size} bytes; the limit is {MAX_FILES_BYTES}")
        for path in files:
            if _size(path) > MAX_PATH_BYTES:
                raise ValueError(f"a path is longer than {MAX_PATH_BYTES} bytes")
            plain = check_relative(path)
            if str(plain) != path:
                raise ValueError(f"{path!r} is not a plain path; write it as {str(plain)!r}")
            if any(_size(part) > MAX_NAME_BYTES for part in plain.parts):
                raise ValueError(f"a name in a path is longer than {MAX_NAME_BYTES} bytes")
            if len(plain.parts) > MAX_FILES_DEPTH:
                raise ValueError(f"a path is more than {MAX_FILES_DEPTH} levels deep")
        directories: set[str] = set()
        for path in files:
            # parents ends with `.`, the workspace itself.
            for parent in PurePosixPath(path).parents[:-1]:
                if str(parent) in files:
                    raise ValueError(f"{str(parent)!r} is given as a file and as a directory")
                directories.add(str(parent))
        if len(files) + len(directories) > MAX_FILES_ENTRIES:
            raise ValueError(
                f"files and their directories are more than {MAX_FILES_ENTRIES:,} entries"
            )
        return files
