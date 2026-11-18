"""The size of a job's workspace, as the watcher of ADR 0014 measures it.

The worker sees the workspace volume at its own mount, so it measures there,
outside the tool container. The walk never follows a symbolic link: every
directory is opened with O_NOFOLLOW relative to its parent's descriptor, so
a command that swaps a directory for a link mid-walk cannot point the walk
elsewhere.
"""

import os
from pathlib import Path

# st_blocks counts 512-byte units, whatever the file system's block size.
_BLOCK = 512
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Unmeasurable(Exception):
    """A part of the workspace could not be read, so its size is unknown."""


def _walk(fd: int) -> int:
    """Bytes on disk below the open directory `fd`, its own entries included."""
    total = 0
    with os.scandir(fd) as entries:
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                # Deleted since the listing: it takes no room any more.
                continue
            total += info.st_blocks * _BLOCK
            if not entry.is_dir(follow_symlinks=False):
                continue
            try:
                child = os.open(entry.name, _DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                continue
            except OSError as error:
                # Unreadable (the command can chmod its own directories), or
                # swapped for a link since the listing.
                raise Unmeasurable(f"{entry.name}: {error.strerror}") from error
            try:
                total += _walk(child)
            finally:
                os.close(child)
    return total


def workspace_size(root: Path) -> int:
    """Bytes the files and directories under `root` take on disk.

    Counts allocated blocks, so a sparse file counts what it really uses. A
    file with several hard links counts once per link. Raises Unmeasurable
    when a directory cannot be opened or listed, including `root` itself.
    """
    try:
        fd = os.open(root, _DIRECTORY)
    except OSError as error:
        raise Unmeasurable(f"{root.name}: {error.strerror}") from error
    try:
        return os.fstat(fd).st_blocks * _BLOCK + _walk(fd)
    except PermissionError as error:
        raise Unmeasurable(f"a directory cannot be listed: {error.strerror}") from error
    except RecursionError as error:
        # Directories nested deeper than Python recurses. Not measured, so
        # treated as over the limit.
        raise Unmeasurable("directories are nested too deep to measure") from error
    finally:
        os.close(fd)
