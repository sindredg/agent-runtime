"""Structured logs: one JSON object per line on stderr.

Also the escapes for text a CI log prints: `json_safe` for a JSON line and
`text_safe` for plain text. Both escape the workflow-command markers.
"""

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

# Attributes every LogRecord has. Anything else on a record was passed with
# `extra=` and belongs in the output.
_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}

# `::` after the start of the text or a line break, and any whitespace.
# Every character Python splits lines at counts as a line break.
_LINE_START = re.compile(r"(?:^|(?<=[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]))(\s*)::")


def json_safe(dumped: str) -> str:
    """JSON text with each `##[` written as `#\\u0023[`.

    `#` occurs only inside a JSON string, so the result is still valid
    JSON and decodes to the same value.
    """
    return dumped.replace("##[", "#\\u0023[")


def text_safe(text: str) -> str:
    """Plain text with `##[` written as `#\\u0023[`, and `::` at the start of a line as `\\u003a:`.

    Line breaks stay as they are. Leading whitespace is kept before the
    escaped `::`.
    """
    escaped = text.replace("##[", "#\\u0023[")
    return _LINE_START.sub(lambda found: found.group(1) + "\\u003a:", escaped)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # default=str: a UUID or a datetime in `extra` is written as text.
        return json_safe(json.dumps(entry, default=str))


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record of the process through JsonFormatter."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # httpx2 logs each answered request with its full URL at INFO. The URL of
    # a notification can hold a token in its path or query (ADR 0029).
    logging.getLogger("httpx2").setLevel(max(level, logging.WARNING))
