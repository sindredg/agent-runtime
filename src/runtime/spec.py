"""The job spec: what a job is asked to do. Stored in `jobs.spec`."""

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from runtime.tools.paths import check_relative

# The spec is one database row. Files that seed the workspace are part of it,
# so they are kept small. The limit counts paths and contents.
MAX_FILES_BYTES = 1024 * 1024
# Limits most file systems have: one name, and a whole path.
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 1024
# Characters in `prompt` and in `system`.
MAX_TEXT_LENGTH = 1_000_000


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

    @field_validator("files")
    @classmethod
    def _files_can_all_be_written(cls, files: dict[str, str]) -> dict[str, str]:
        """Refuse a set of files that could not all exist side by side.

        Each path has to be in its plain form, so two spellings of one path
        (`a` and `./a`) cannot both be given. No path may be a directory of
        another (`a` and `a/b`).
        """
        for path in files:
            plain = check_relative(path)
            if str(plain) != path:
                raise ValueError(f"{path!r} is not a plain path; write it as {str(plain)!r}")
            if _size(path) > MAX_PATH_BYTES:
                raise ValueError(f"a path is longer than {MAX_PATH_BYTES} bytes")
            if any(_size(part) > MAX_NAME_BYTES for part in plain.parts):
                raise ValueError(f"a name in a path is longer than {MAX_NAME_BYTES} bytes")
        for path in files:
            for parent in PurePosixPath(path).parents:
                if str(parent) in files:
                    raise ValueError(f"{str(parent)!r} is given as a file and as a directory")
        size = sum(_size(path) + _size(content) for path, content in files.items())
        if size > MAX_FILES_BYTES:
            raise ValueError(f"files hold {size} bytes; the limit is {MAX_FILES_BYTES}")
        return files
