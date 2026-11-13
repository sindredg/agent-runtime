"""The job workspace: the one directory a job's tools may read."""

import shutil
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
        relative = check_relative(name)
        # Directories are made one level at a time, each checked first. A
        # link that leads out is refused before anything is created past it.
        for parent in reversed(relative.parents[:-1]):
            resolve_in_workspace(workspace, str(parent)).mkdir(exist_ok=True)
        target = resolve_in_workspace(workspace, name)
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    return workspace


def remove_workspace(root: Path, job_id: UUID) -> None:
    """Delete `root/<job_id>`. A workspace that is not there is not an error."""
    shutil.rmtree(root / str(job_id), ignore_errors=True)
