"""The job spec's validation and the workspace it seeds."""

import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from runtime.spec import MAX_FILES_BYTES, MAX_NAME_BYTES, MAX_TEXT_LENGTH, JobSpec
from runtime.tools.paths import PathNotAllowed
from runtime.worker.workspace import prepare_workspace, remove_workspace

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
        {"prompt": "go", "files": {"a": "x" * MAX_FILES_BYTES}},
        {"prompt": "go", "files": {"a": "x" * (MAX_FILES_BYTES - 2), "b": "y"}},
        # Two spellings of one path, or a path that is not in its plain form.
        {"prompt": "go", "files": {"./a": "x"}},
        {"prompt": "go", "files": {"a/": "x"}},
        {"prompt": "go", "files": {"a//b": "x"}},
        {"prompt": "go", "files": {"a/./b": "x"}},
        # One path is a directory of another.
        {"prompt": "go", "files": {"a": "x", "a/b": "y"}},
        {"prompt": "go", "files": {"a/b/c": "x", "a": "y"}},
        {"prompt": "go", "files": {"n" * (MAX_NAME_BYTES + 1): "x"}},
        {"prompt": "go", "files": {"/".join(["d" * 200] * 6): "x"}},
        {"prompt": "x" * (MAX_TEXT_LENGTH + 1)},
        {"prompt": "go", "system": "x" * (MAX_TEXT_LENGTH + 1)},
        {"prompt": "go", "model": "m" * 201},
    ],
)
def test_spec_refuses(spec: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate(spec)


def test_files_at_the_size_limit_are_accepted() -> None:
    """The limit counts the path and the content: 1 byte of path here."""
    JobSpec.model_validate({"prompt": "go", "files": {"a": "x" * (MAX_FILES_BYTES - 1)}})
    JobSpec.model_validate({"prompt": "go", "files": {"n" * MAX_NAME_BYTES: "x"}})
    JobSpec.model_validate({"prompt": "x" * MAX_TEXT_LENGTH})


def test_sibling_files_and_nested_directories_are_accepted() -> None:
    files = {"a/b": "1", "a/c": "2", "ab": "3", "a.txt": "4", "d/e/f": "5"}
    assert JobSpec.model_validate({"prompt": "go", "files": files}).files == files


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


def everything_under(directory: Path) -> set[str]:
    return {str(path.relative_to(directory)) for path in directory.rglob("*")}


@pytest.mark.parametrize("name", ["../escape.txt", "/escape.txt", "a/../../escape.txt"])
def test_workspace_refuses_a_file_outside_it(tmp_path: Path, name: str) -> None:
    with pytest.raises(PathNotAllowed):
        prepare_workspace(tmp_path / "root", JOB, {name: "x"})
    # Nothing was written anywhere: only the empty workspace exists.
    assert everything_under(tmp_path) == {"root", f"root/{JOB}"}
    assert not Path("/escape.txt").exists()


@pytest.mark.parametrize("name", ["out/escape.txt", "out/made/by/seeding/escape.txt"])
def test_workspace_creates_nothing_through_a_symlink_that_leaves_it(
    tmp_path: Path, name: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = prepare_workspace(tmp_path / "root", JOB, {})
    os.symlink(outside, workspace / "out")
    with pytest.raises(PathNotAllowed):
        prepare_workspace(tmp_path / "root", JOB, {name: "x"})
    # Not the file, and not the directories on the way to it.
    assert list(outside.iterdir()) == []


def test_remove_workspace_deletes_one_job_and_nothing_else(tmp_path: Path) -> None:
    other = UUID("00000000-0000-7000-8000-000000000002")
    prepare_workspace(tmp_path, JOB, {"a/b.txt": "x"})
    kept = prepare_workspace(tmp_path, other, {"f.txt": "y"})
    remove_workspace(tmp_path, JOB)
    assert everything_under(tmp_path) == {str(other), f"{other}/f.txt"}
    assert (kept / "f.txt").read_text() == "y"
    # Removing it again, or one that never existed, is not an error.
    remove_workspace(tmp_path, JOB)
