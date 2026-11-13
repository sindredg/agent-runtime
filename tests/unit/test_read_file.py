"""read_file stays inside the workspace and returns text or a clear error."""

import os
from pathlib import Path

import pytest

from runtime.tools import default_tools
from runtime.tools.base import ToolResult
from runtime.tools.paths import PathNotAllowed, check_relative, resolve_in_workspace
from runtime.tools.read_file import MAX_BYTES, READ_FILE


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace with files, beside a secret that is outside it."""
    (tmp_path / "secret.txt").write_text("outside")
    (tmp_path / "outside-dir").mkdir()
    (tmp_path / "outside-dir" / "secret.txt").write_text("outside")
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    (root / "hello.txt").write_text("Hello!\n")
    (root / "sub" / "deep.txt").write_text("deep")
    return root


def read(workspace: Path, path: object) -> ToolResult:
    return READ_FILE.run(workspace, {"path": path})


def test_reads_a_file_in_the_workspace(workspace: Path) -> None:
    assert read(workspace, "hello.txt") == ToolResult("Hello!\n")
    assert read(workspace, "sub/deep.txt") == ToolResult("deep")
    assert read(workspace, "./sub/./deep.txt") == ToolResult("deep")


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "sub/../../secret.txt",
        "sub/../hello.txt",
        "..",
        "/etc/passwd",
        "/",
        "",
        ".",
        "hello.txt\x00.png",
    ],
)
def test_refuses_dotdot_absolute_and_empty_paths(workspace: Path, path: str) -> None:
    result = read(workspace, path)
    assert result.is_error
    assert "path not allowed" in result.content
    assert "outside" not in result.content


def test_refuses_an_absolute_path_to_a_file_inside_the_workspace(workspace: Path) -> None:
    result = read(workspace, str(workspace / "hello.txt"))
    assert result.is_error
    assert "absolute" in result.content


@pytest.mark.parametrize(
    ("link", "target"),
    [
        ("link.txt", "../secret.txt"),
        ("abs-link.txt", "ABSOLUTE"),
        ("linked-dir", "../outside-dir"),
        ("sub/out", "../.."),
    ],
)
def test_refuses_a_symlink_that_leaves_the_workspace(
    workspace: Path, link: str, target: str
) -> None:
    if target == "ABSOLUTE":
        target = str(workspace.parent / "secret.txt")
    os.symlink(target, workspace / link)
    # None of the requested paths holds `..`: only the symlink check can refuse them.
    path = {"linked-dir": "linked-dir/secret.txt", "sub/out": "sub/out/secret.txt"}.get(link, link)
    assert ".." not in path
    result = read(workspace, path)
    assert result.is_error
    assert "path not allowed" in result.content
    assert "outside" not in result.content


def test_a_symlinked_directory_that_leaves_is_refused_without_dotdot(workspace: Path) -> None:
    os.symlink(workspace.parent, workspace / "parent")
    result = read(workspace, "parent/secret.txt")
    assert result.is_error
    assert "leads out of the workspace" in result.content


def test_follows_a_symlink_that_stays_inside(workspace: Path) -> None:
    os.symlink("sub/deep.txt", workspace / "inside-link.txt")
    assert read(workspace, "inside-link.txt") == ToolResult("deep")


def test_a_workspace_that_is_itself_a_symlink_still_works(workspace: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    os.symlink(workspace, alias)
    assert read(alias, "hello.txt") == ToolResult("Hello!\n")
    assert read(alias, "../secret.txt").is_error


@pytest.mark.parametrize("path", ["missing.txt", "sub", "sub/missing/deeper.txt"])
def test_a_missing_file_or_a_directory_is_an_error_result(workspace: Path, path: str) -> None:
    result = read(workspace, path)
    assert result.is_error
    assert "no such file" in result.content


@pytest.mark.parametrize("tool_input", [{}, {"path": 5}, {"path": None}, {"file": "hello.txt"}])
def test_input_without_a_string_path_is_an_error_result(
    workspace: Path, tool_input: dict[str, object]
) -> None:
    assert READ_FILE.run(workspace, tool_input).is_error


def test_size_limit(workspace: Path) -> None:
    (workspace / "at-limit.txt").write_bytes(b"a" * MAX_BYTES)
    (workspace / "over.txt").write_bytes(b"a" * (MAX_BYTES + 1))
    assert read(workspace, "at-limit.txt") == ToolResult("a" * MAX_BYTES)
    over = read(workspace, "over.txt")
    assert over.is_error
    assert str(MAX_BYTES + 1) in over.content


@pytest.mark.parametrize("data", [b"\xff\xfe\x00binary", b"text\x00with nul", b"caf\xe9 latin-1"])
def test_a_file_that_is_not_text_is_an_error_result(workspace: Path, data: bytes) -> None:
    (workspace / "blob.bin").write_bytes(data)
    result = read(workspace, "blob.bin")
    assert result.is_error
    assert "not a" in result.content


def test_check_relative_and_resolve_agree_on_plain_paths(workspace: Path) -> None:
    assert str(check_relative("sub/deep.txt")) == "sub/deep.txt"
    assert resolve_in_workspace(workspace, "sub/deep.txt") == (workspace / "sub/deep.txt").resolve()
    with pytest.raises(PathNotAllowed):
        check_relative("a/../b")


def test_default_tools_offer_read_file_with_a_schema() -> None:
    (definition,) = default_tools().definitions()
    assert definition["name"] == "read_file"
    assert definition["input_schema"]["required"] == ["path"]
    assert default_tools().get("read_file") is READ_FILE
    assert default_tools().get("run_shell") is None
