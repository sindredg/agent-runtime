"""The size of a job's workspace, as the watcher of ADR 0014 and ADR 0017 measures it.

The worker sees the workspace volume at its own mount, so it measures there,
outside the tool container. The walk never follows a symbolic link: every
directory is opened with O_NOFOLLOW relative to its parent's descriptor, so
a command that swaps a directory for a link mid-walk cannot point the walk
elsewhere.

The walk is bounded: it stops at a number of entries, a depth and a deadline,
and as soon as the size passes the limit. A command could otherwise make it
slow enough that no measurement ends during a call.

Not seen by the walk: a file deleted while a process holds it open, and a
file moved from a directory not walked yet into one walked already.
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path

# st_blocks counts 512-byte units, whatever the file system's block size.
_BLOCK = 512
_MIB = 1024 * 1024
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# Directories below the workspace the walk descends into. Each level holds
# two descriptors while the walk is below it.
MAX_DEPTH = 256
# The deadline is checked once per this many entries.
_CHECK_EVERY = 1024


class Unmeasurable(Exception):
    """The workspace's size is unknown: unreadable, too deep, or too slow to walk."""


class OverLimit(Exception):
    """The walk stopped early: the workspace is over a limit already."""


@dataclass(frozen=True, slots=True)
class Bounds:
    """Where a walk stops. Passing `size` or `entries` raises OverLimit."""

    size: int
    entries: int
    # time.monotonic() after which the walk raises Unmeasurable.
    deadline: float


class _Walk:
    def __init__(self, bounds: Bounds) -> None:
        self.bounds = bounds
        self.size = 0
        self.entries = 0

    def add(self, size: int) -> None:
        self.size += size
        self.entries += 1
        if self.size > self.bounds.size:
            raise OverLimit(f"holds more than its limit of {self.bounds.size / _MIB:.1f} MiB")
        if self.entries > self.bounds.entries:
            raise OverLimit(
                f"holds more than its limit of {self.bounds.entries} files and directories"
            )
        if self.entries % _CHECK_EVERY == 0 and time.monotonic() > self.bounds.deadline:
            raise Unmeasurable(f"the walk took too long, after {self.entries} entries")

    def directory(self, fd: int, depth: int) -> None:
        """Add every entry below the open directory `fd`."""
        with os.scandir(fd) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    # Deleted since the listing: it takes no room any more.
                    continue
                self.add(info.st_blocks * _BLOCK)
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if depth >= MAX_DEPTH:
                    raise Unmeasurable(f"directories are nested deeper than {MAX_DEPTH}")
                try:
                    child = os.open(entry.name, _DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    continue
                try:
                    self.directory(child, depth + 1)
                finally:
                    os.close(child)


def workspace_size(root: Path, bounds: Bounds) -> int:
    """Bytes the files and directories under `root` take on disk.

    Counts allocated blocks, so a sparse file counts what it really uses. A
    file with several hard links counts once per link. Raises OverLimit as
    soon as `bounds` is passed, and Unmeasurable when a directory cannot be
    opened or listed (including `root`), is nested too deep, or the walk
    passes its deadline. Any other error of the file system is Unmeasurable
    too: a command can make the walk fail, so a failed walk must not pass.
    """
    walk = _Walk(bounds)
    try:
        fd = os.open(root, _DIRECTORY)
    except OSError as error:
        raise Unmeasurable(f"{root.name}: {error.strerror}") from error
    try:
        walk.add(os.fstat(fd).st_blocks * _BLOCK)
        walk.directory(fd, 1)
    except OSError as error:
        # Unreadable (the command runs as the worker's uid and may chmod its
        # own directories), swapped for a link, or out of descriptors.
        raise Unmeasurable(error.strerror or type(error).__name__) from error
    finally:
        os.close(fd)
    return walk.size
