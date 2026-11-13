"""Log records come out as one JSON object per line."""

import io
import json
import logging
from uuid import UUID

from runtime.logs import JsonFormatter


def emit(log: logging.Logger, stream: io.StringIO) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)


def test_record_is_one_json_line_with_its_extra_fields() -> None:
    stream = io.StringIO()
    log = logging.getLogger("test.logs.one")
    emit(log, stream)
    job_id = UUID("00000000-0000-7000-8000-000000000001")
    log.info("step %s", "written", extra={"job_id": job_id, "seq": 3, "kind": "tool_result"})

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["message"] == "step written"
    assert entry["level"] == "info"
    assert entry["logger"] == "test.logs.one"
    assert entry["job_id"] == str(job_id)
    assert entry["seq"] == 3
    assert entry["kind"] == "tool_result"
    assert entry["time"].endswith("+00:00")


def test_an_exception_stays_on_the_same_line() -> None:
    stream = io.StringIO()
    log = logging.getLogger("test.logs.two")
    emit(log, stream)
    try:
        raise ValueError("line one\nline two")
    except ValueError:
        log.exception("failed")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["level"] == "error"
    assert "ValueError: line one\nline two" in entry["exception"]
