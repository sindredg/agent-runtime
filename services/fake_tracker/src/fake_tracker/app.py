"""The fake issue tracker: the outside system that the `create_issue` tool changes.

It stores every issue it is sent, with the operation id the caller gave, and
never merges two. An issue that was created twice is therefore visible here as
two entries. Tests read the list back as the record of what really happened
outside the runtime, independent of the runtime's own step log.

Everything is kept in memory and is gone when the service stops.
"""

import json
from datetime import UTC, datetime
from itertools import count
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

# Characters in an issue body, and issues kept. Both only bound the memory a
# test run can use up.
MAX_BODY_LENGTH = 64 * 1024
MAX_ISSUES = 100_000


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


def create_app() -> FastAPI:
    app = FastAPI(title="fake-tracker")
    issues: list[dict[str, Any]] = []
    numbers = count(1)

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
        if len(issues) >= MAX_ISSUES:
            return _json({"detail": "the tracker is full; clear it with DELETE /issues"}, 507)
        entry = {
            "id": next(numbers),
            "title": new.title,
            "body": new.body,
            "operation_id": new.operation_id,
            "created_at": datetime.now(UTC).isoformat(),
        }
        issues.append(entry)
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
