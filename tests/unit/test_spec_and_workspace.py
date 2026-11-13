"""The job spec's validation and the workspace it seeds."""

import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from runtime.spec import MAX_FILES_BYTES, JobSpec
from runtime.tools.paths import PathNotAllowed
from runtime.worker.workspace import prepare_workspace

JOB = UUID("00000000-0000-7000-8000-000000000001")


def test_minimal_spec() -> None:
    spec = JobSpec.model_validate({"prompt": "go"})
    assert (spec.prompt, spec.system, spec.model, spec.files) == ("go", None, None, {})


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"prompt": ""},
        {"prompt": 5},
        {"prompt": "go", "system": ""},
        {"prompt": "go", "model": ""},
        {"prompt": "go", "tools": []},
        {"prompt": "go", "files": {"../a": "x"}},
        {"prompt": "go", "files": {"/abs": "x"}},
        {"prompt": "go", "files": {"": "x"}},
        {"prompt": "go", "files": {"a": 5}},
        {"prompt": "go", "files": {"a": "x" * (MAX_FILES_BYTES + 1)}},
        {"prompt": "go", "files": {"a": "x" * MAX_FILES_BYTES, "b": "y"}},
    ],
)
def test_spec_refuses(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate(spec)


def test_files_at_the_size_limit_are_accepted() -> None:
    JobSpec.model_validate({"prompt": "go", "files": {"a": "x" * MAX_FILES_BYTES}})


def test_workspace_gets_the_files_and_keeps_existing_ones(tmp_path: Path) -> None:
    files = {"hello.txt": "Hi\n", "a/b/c.txt": "deep"}
    workspace = prepare_workspace(tmp_path, JOB, files)
    assert workspace == tmp_path / str(JOB)
    assert (workspace / "hello.txt").read_text() == "Hi\n"
    assert (workspace / "a" / "b" / "c.txt").read_text() == "deep"

    # A second preparation leaves a changed file as it is.
    (workspace / "hello.txt").write_text("edited")
    prepare_workspace(tmp_path, JOB, files)
    assert (workspace / "hello.txt").read_text() == "edited"


def test_workspaces_of_two_jobs_are_separate(tmp_path: Path) -> None:
    other = UUID("00000000-0000-7000-8000-000000000002")
    one = prepare_workspace(tmp_path, JOB, {"f.txt": "one"})
    two = prepare_workspace(tmp_path, other, {"f.txt": "two"})
    assert one != two
    assert (one / "f.txt").read_text() == "one"


@pytest.mark.parametrize("name", ["../escape.txt", "/escape.txt", "a/../../escape.txt"])
def test_workspace_refuses_a_file_outside_it(tmp_path: Path, name: str) -> None:
    with pytest.raises(PathNotAllowed):
        prepare_workspace(tmp_path / "root", JOB, {name: "x"})
    assert not (tmp_path / "escape.txt").exists()


def test_workspace_does_not_write_through_a_symlink_that_leaves_it(tmp_path: Path) -> None:
    workspace = prepare_workspace(tmp_path / "root", JOB, {})
    os.symlink(tmp_path, workspace / "out")
    with pytest.raises(PathNotAllowed):
        prepare_workspace(tmp_path / "root", JOB, {"out/escape.txt": "x"})
    assert not (tmp_path / "escape.txt").exists()
