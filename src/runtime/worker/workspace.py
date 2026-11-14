"""The job workspace: the one directory a job's tools may read and write."""

import os
import shutil
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from uuid import UUID

from runtime.tools.paths import check_relative


def _staging(root: Path, job_id: UUID) -> Path:
    # Starts with a dot, so it is never a job's directory.
    return root / f".{job_id}.new"


def prepare_workspace(root: Path, job_id: UUID, files: Mapping[str, str]) -> Path:
    """Return `root/<job_id>`, made with the spec's files if it does not exist yet.

    The workspace is filled in a staging directory and renamed into place,
    so it appears whole or not at all. Once it exists it is left exactly as
    the job's tools left it: files changed, removed, replaced by links or
    made unreadable by a command do not stop a worker that claims the job
    later.
    """
    # Every name is checked before anything is created.
    relatives = {name: check_relative(name) for name in files}
    workspace = root / str(job_id)
    if workspace.is_dir() and not workspace.is_symlink():
        return workspace
    staging = _staging(root, job_id)
    # Left over from a worker that stopped half way.
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name, relative in relatives.items():
        target = staging / relative
        # The staging directory is new and only this worker writes to it, so
        # nothing in it is a link.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[name], encoding="utf-8")
    staging.rename(workspace)
    return workspace


def _give_owner_access(top: Path) -> None:
    """Let the owner into every directory under `top` again.

    A command runs as the worker's uid and can take its own permissions
    away, which would keep the directory from being removed.
    """
    with suppress(OSError):
        top.chmod(0o700)
    for directory, names, _ in os.walk(top):
        for name in names:
            path = os.path.join(directory, name)
            # A link is not followed: its target may be outside the workspace.
            if not os.path.islink(path):
                with suppress(OSError):
                    os.chmod(path, 0o700)


def remove_workspace(root: Path, job_id: UUID) -> None:
    """Delete `root/<job_id>` and its staging directory. One that is not there is not an error."""
    for path in (root / str(job_id), _staging(root, job_id)):
        shutil.rmtree(path, ignore_errors=True)
        if path.exists() and not path.is_symlink():
            _give_owner_access(path)
            shutil.rmtree(path, ignore_errors=True)
