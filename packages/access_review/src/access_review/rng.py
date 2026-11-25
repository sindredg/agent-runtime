"""The shapes of a job and the random stream every draw comes from.

There is no `random`: a stream is SHA-256 over the seed, the generator
version, the shape and a label, so one seed gives one job on any machine.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

# Goes up with any change to what a job is sent or to a key.
VERSION: int = 1

# One stream per label. A file's fill has a stream of its own.
LABELS = ("systems", "teams", "people", "grant ids", "placement", "notices", "code")
_FILL_LABEL = re.compile(r"fill [0-9]{4}")


def fill_label(row: int) -> str:
    """The label of the stream that fills the file at `row`."""
    return f"fill {row:04d}"


def rows(n: int, first: int, k: int) -> list[int]:
    """`k` rows spread evenly over the rows `first` to `n - 1`."""
    return [first + ((2 * i + 1) * (n - first)) // (2 * k) for i in range(k)]


# Decided grants per shape: the rows `rows(files, lead_rows, DECIDED)`.
DECIDED = 16


@dataclass(frozen=True)
class Shape:
    """How many files a job has, and how many rows lead its decided grants."""

    name: str
    files: int
    lead_rows: int

    def __post_init__(self) -> None:
        if len(set(rows(self.files, self.lead_rows, DECIDED))) != DECIDED:
            raise ValueError("the rows of the decided grants are not all different")


FULL = Shape("full", 293, 24)
SHORT = Shape("short", 36, 6)
SHAPES = {shape.name: shape for shape in (FULL, SHORT)}


class Source(Protocol):
    """Anything that draws a number below `n`."""

    def below(self, n: int) -> int: ...


class Stream:
    """The random numbers of one label, for one seed and one shape."""

    def __init__(self, seed: bytes, shape: Shape, label: str) -> None:
        if label not in LABELS and not _FILL_LABEL.fullmatch(label):
            raise ValueError("not a stream label")
        self._prefix = b"\x00".join(
            (
                b"access-review",
                str(VERSION).encode(),
                shape.name.encode(),
                label.encode(),
                seed,
            )
        )
        self._block = 0
        self._buffer = b""

    def _take(self) -> int:
        if len(self._buffer) < 8:
            block = self._prefix + self._block.to_bytes(8, "big")
            self._buffer += hashlib.sha256(block).digest()
            self._block += 1
        value = int.from_bytes(self._buffer[:8], "big")
        self._buffer = self._buffer[8:]
        return value

    def below(self, n: int) -> int:
        """A number from 0 to `n - 1`, each equally likely."""
        if n < 1:
            raise ValueError("nothing to draw from")
        limit = (2**64 // n) * n
        while True:
            value = self._take()
            if value < limit:
                return value % n

    def pick[T](self, items: Sequence[T]) -> T:
        return items[self.below(len(items))]

    def shuffle[T](self, items: Sequence[T]) -> list[T]:
        """A new list of `items` in a drawn order (Fisher-Yates)."""
        out = list(items)
        for i in range(len(out) - 1, 0, -1):
            j = self.below(i + 1)
            out[i], out[j] = out[j], out[i]
        return out


def draw_day(source: Source, first: date, last: date) -> date:
    """A day from `first` to `last`, both included."""
    return first + timedelta(days=source.below((last - first).days + 1))
