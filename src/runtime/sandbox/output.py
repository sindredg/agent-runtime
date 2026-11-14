"""A container's output, read from Docker's attach stream and cut at a limit.

Without a terminal, Docker sends stdout and stderr on one connection in
frames: one byte for the stream (1 stdout, 2 stderr), three zero bytes, the
payload size as a 32-bit big-endian number, then the payload. A frame can be
split across the chunks the connection delivers.
"""

HEADER_SIZE = 8
_STREAMS = frozenset({1, 2})


class FramingError(Exception):
    """The bytes are not Docker's frames."""


class Output:
    """The output of one command: stdout and stderr in the order they arrived.

    Only the first `limit` bytes are kept. The rest is counted and dropped,
    so a command that prints without end costs no memory.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.kept = bytearray()
        # Payload bytes received, kept or not.
        self.total = 0
        self._header = bytearray()
        # Payload bytes of the current frame still to come.
        self._left = 0

    @property
    def truncated(self) -> bool:
        return self.total > self.limit

    def feed(self, chunk: bytes) -> None:
        """Take the next chunk of the stream. Raises FramingError on a bad header."""
        view = memoryview(chunk)
        while view:
            if self._left == 0:
                needed = HEADER_SIZE - len(self._header)
                self._header += view[:needed]
                view = view[needed:]
                if len(self._header) < HEADER_SIZE:
                    return
                if self._header[0] not in _STREAMS or any(self._header[1:4]):
                    raise FramingError(f"not a frame header: {bytes(self._header)!r}")
                self._left = int.from_bytes(self._header[4:], "big")
                self._header.clear()
                continue
            data = view[: self._left]
            view = view[len(data) :]
            self._left -= len(data)
            self.total += len(data)
            room = self.limit - len(self.kept)
            if room > 0:
                self.kept += data[:room]
