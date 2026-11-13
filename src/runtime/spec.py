"""The job spec: what a job is asked to do. Stored in `jobs.spec`."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from runtime.tools.paths import check_relative

# The spec is one database row. Files that seed the workspace are part of it,
# so they are kept small.
MAX_FILES_BYTES = 1024 * 1024


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # The first user message.
    prompt: str = Field(min_length=1)
    system: str | None = Field(default=None, min_length=1)
    # Left out, the worker's configured model is used.
    model: str | None = Field(default=None, min_length=1)
    # Text files written into the job's workspace before it starts:
    # relative path -> content.
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _files_are_small_and_relative(cls, files: dict[str, str]) -> dict[str, str]:
        for path in files:
            check_relative(path)
        # surrogatepass: text that is not valid Unicode is still measured
        # here. The store refuses it with its own error.
        size = sum(len(content.encode("utf-8", "surrogatepass")) for content in files.values())
        if size > MAX_FILES_BYTES:
            raise ValueError(f"files hold {size} bytes; the limit is {MAX_FILES_BYTES}")
        return files
