"""The fake issue tracker: the outside system that the `create_issue` tool changes.

It stores every issue it is sent, with the operation id the caller gave, and
never merges two. An issue that was created twice is therefore visible here as
two entries. Tests read the list back as the record of what really happened
outside the runtime, independent of the runtime's own step log.

Everything is kept in memory and is gone when the service stops.

FAKE_TRACKER_FIRST_ISSUE sets the first issue number. Set, it must have as
many digits as the MAX_ISSUES-th number, which is the last the tracker gives:
a post past it is refused with 507, also after a clear, since a clear does
not restart the numbering. So every number has one width, and a number in a
tool result does not change the length of the text. Unset, numbering starts
at 1 and goes on after a clear.
"""

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from allowed_hosts import AllowedHosts

# Characters in an issue body, and issues kept. Both only bound the memory a
# test run can use up.
MAX_BODY_LENGTH = 64 * 1024
MAX_ISSUES = 100_000
# The names the stack and the tests reach it by. Any other Host is refused,
# so a web page that points its own name at 127.0.0.1 cannot use it.
HOSTS = ("fake-tracker", "localhost", "127.0.0.1")


class NewIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=MAX_BODY_LENGTH)
    # Names the tool call that creates the issue. The same call sends the
    # same id every time it runs.
    operation_id: str = Field(min_length=1, max_length=200)


def _json(content: Any, status: int = 200) -> Response:
    # json.dumps escapes everything outside ASCII, so text that is not valid
    # Unicode, which a request can carry, does not break the answer.
    return Response(content=json.dumps(content), status_code=status, media_type="application/json")


def first_issue(env: Mapping[str, str]) -> int:
    """The first issue number: FAKE_TRACKER_FIRST_ISSUE, or 1 when it is unset or empty.

    Raises ValueError for a value that is not a positive whole number, or
    whose width differs from that of the last number a full tracker holds.
    """
    value = env.get("FAKE_TRACKER_FIRST_ISSUE")
    if not value:
        return 1
    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise ValueError(f"FAKE_TRACKER_FIRST_ISSUE must be a positive whole number, not {value!r}")
    first = int(value)
    last = first + MAX_ISSUES - 1
    if len(str(first)) != len(str(last)):
        raise ValueError(
            f"FAKE_TRACKER_FIRST_ISSUE {first} has {len(str(first))} digits and the last"
            f" number, {last}, has {len(str(last))}: the numbers would not have one width"
        )
    return first


def create_app(env: Mapping[str, str] | None = None) -> FastAPI:
    """The app. Reads FAKE_TRACKER_FIRST_ISSUE from `env`, or from the environment."""
    env = os.environ if env is None else env
    first = first_issue(env)
    # Set, the last number the tracker gives. Unset, there is none.
    last = first + MAX_ISSUES - 1 if env.get("FAKE_TRACKER_FIRST_ISSUE") else None
    app = FastAPI(title="fake-tracker")
    app.add_middleware(AllowedHosts, hosts=HOSTS)
    issues: list[dict[str, Any]] = []
    next_number = first

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> Response:
        """422 with what is wrong and where, without echoing the input back."""
        detail = [
            {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return Response(
            content=json.dumps({"detail": detail}, default=str),
            status_code=422,
            media_type="application/json",
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/issues", status_code=201)
    async def create_issue(new: NewIssue) -> Response:
        nonlocal next_number
        if len(issues) >= MAX_ISSUES:
            return _json({"detail": "the tracker is full; clear it with DELETE /issues"}, 507)
        if last is not None and next_number > last:
            # The next number would be wider than the first. A clear does not
            # help, so the answer says to restart the tracker instead.
            return _json({"detail": "the tracker has given its last number; restart it"}, 507)
        entry = {
            "id": next_number,
            "title": new.title,
            "body": new.body,
            "operation_id": new.operation_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        issues.append(entry)
        next_number += 1
        return _json(entry, 201)

    @app.get("/issues")
    async def list_issues(
        operation_id: Annotated[str | None, Query(max_length=200)] = None,
    ) -> Response:
        """Every issue, oldest first. `operation_id` keeps only the issues of one call."""
        return _json([entry for entry in issues if operation_id in (None, entry["operation_id"])])

    @app.delete("/issues", status_code=204)
    async def clear_issues() -> None:
        """Forget every issue. Numbering goes on, so an id never names two issues."""
        issues.clear()

    return app


app = create_app()
