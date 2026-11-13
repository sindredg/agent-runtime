"""Keeping file paths inside a job's workspace."""

from pathlib import Path, PurePosixPath


class PathNotAllowed(ValueError):
    """The path is not a plain relative path, or it leads out of the workspace."""


def check_relative(path: str) -> PurePosixPath:
    """Accept only a relative path with no `..` part. Does not touch the disk."""
    if not path or "\x00" in path:
        raise PathNotAllowed("the path is empty or contains a NUL character")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise PathNotAllowed(f"{path!r} is absolute; paths are relative to the workspace")
    if ".." in pure.parts:
        raise PathNotAllowed(f"{path!r} contains '..'")
    if not pure.parts:
        raise PathNotAllowed(f"{path!r} does not name a file")
    return pure


def resolve_in_workspace(workspace: Path, path: str) -> Path:
    """The real location of `path` inside `workspace`.

    Symlinks are followed first and the result is checked second, so a link
    that points out of the workspace is refused, wherever in the path it is.
    """
    relative = check_relative(path)
    root = workspace.resolve(strict=True)
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise PathNotAllowed(f"{path!r} leads out of the workspace")
    return target
