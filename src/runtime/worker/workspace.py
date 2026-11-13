"""The job workspace: the one directory a job's tools may read."""

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from runtime.tools.paths import check_relative, resolve_in_workspace


def prepare_workspace(root: Path, job_id: UUID, files: Mapping[str, str]) -> Path:
    """Create `root/<job_id>` and write the spec's files into it.

    A file that is already there is left alone, so preparing the same
    workspace twice changes nothing.
    """
    workspace = root / str(job_id)
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (workspace / check_relative(name)).parent.mkdir(parents=True, exist_ok=True)
        target = resolve_in_workspace(workspace, name)
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    return workspace
