"""Which directories under the workspace root the sweep takes for a job's."""

import uuid
from pathlib import Path

from runtime.worker.sweep import workspace_jobs

JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")
STAGED = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947907")


def test_job_directories_and_staging_directories_are_found(tmp_path: Path) -> None:
    (tmp_path / str(JOB)).mkdir()
    (tmp_path / f".{STAGED}.new").mkdir()

    assert workspace_jobs(tmp_path) == {JOB, STAGED}


def test_anything_else_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / "lost+found").mkdir()
    (tmp_path / ".cache").mkdir()
    # Not the canonical spelling of a job id.
    (tmp_path / str(JOB).upper()).mkdir()
    (tmp_path / JOB.hex).mkdir()
    # A file, and a link to a directory, with a job's name.
    (tmp_path / str(uuid.uuid4())).write_text("not a directory")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / str(uuid.uuid4())).symlink_to(target)

    assert workspace_jobs(tmp_path) == set()


def test_a_root_that_does_not_exist_has_no_jobs(tmp_path: Path) -> None:
    assert workspace_jobs(tmp_path / "missing") == set()
