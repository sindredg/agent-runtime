"""Reading a container's output from Docker's stream: the frames and the cut."""

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from runtime.sandbox.docker import SandboxError
from runtime.sandbox.output import FramingError, Output


def frame(stream: int, data: bytes) -> bytes:
    """One frame as Docker sends it: stream type, three zero bytes, size, payload."""
    return struct.pack(">BxxxI", stream, len(data)) + data


def read(chunks: list[bytes], limit: int = 100) -> Output:
    output = Output(limit)
    for chunk in chunks:
        output.feed(chunk)
    return output


def test_stdout_and_stderr_are_kept_in_the_order_they_arrived() -> None:
    output = read([frame(1, b"out\n"), frame(2, b"err\n"), frame(1, b"more\n")])
    assert output.kept == b"out\nerr\nmore\n"
    assert output.total == 13
    assert not output.truncated


def test_output_over_the_limit_is_cut_and_counted() -> None:
    output = read([frame(1, b"a" * 60), frame(2, b"b" * 60)], limit=100)
    assert output.kept == b"a" * 60 + b"b" * 40
    assert output.total == 120
    assert output.truncated


def test_output_of_exactly_the_limit_is_not_cut() -> None:
    output = read([frame(1, b"a" * 100)], limit=100)
    assert output.kept == b"a" * 100
    assert not output.truncated


def test_a_frame_of_size_zero_is_skipped() -> None:
    assert read([frame(1, b""), frame(1, b"x")]).kept == b"x"


@pytest.mark.parametrize(
    "header",
    [
        # stdin is never attached, so a frame for it is not Docker's framing.
        struct.pack(">BxxxI", 0, 1),
        struct.pack(">BxxxI", 3, 1),
        # The three bytes after the type are zero in every frame.
        b"\x01\x00\x01\x00\x00\x00\x00\x01",
    ],
)
def test_bytes_that_are_not_a_frame_header_are_refused(header: bytes) -> None:
    # A sandbox error: the tool turns it into an error result.
    assert issubclass(FramingError, SandboxError)
    with pytest.raises(FramingError):
        read([header + b"x"])


@given(
    frames=st.lists(st.tuples(st.sampled_from([1, 2]), st.binary(max_size=300)), max_size=8),
    cuts=st.lists(st.integers(min_value=0, max_value=3000), max_size=10),
    limit=st.integers(min_value=0, max_value=1000),
)
def test_the_result_does_not_depend_on_where_the_stream_is_split(
    frames: list[tuple[int, bytes]], cuts: list[int], limit: int
) -> None:
    stream = b"".join(frame(kind, data) for kind, data in frames)
    payload = b"".join(data for _, data in frames)
    points = sorted({min(cut, len(stream)) for cut in cuts})
    chunks = [stream[a:b] for a, b in zip([0, *points], [*points, len(stream)], strict=True)]

    output = read(chunks, limit=limit)

    assert output.kept == payload[:limit]
    assert output.total == len(payload)
    assert output.truncated == (len(payload) > limit)
