"""create_issue: open an issue in an issue tracker.

The one tool here that changes something outside the runtime. It sends the
call's operation id with the issue and brings a check that looks the id up,
so a call that was interrupted is not repeated when the issue already exists.
"""

from typing import Any

import httpx2

from runtime.tools.base import CheckAnswer, CheckOutcome, Tool, ToolCall, ToolResult

# Seconds for one request to the tracker.
TIMEOUT = 10.0


def _created(issue: Any) -> ToolResult:
    """The result for an issue the tracker holds. Raises if the entry is not an issue."""
    return ToolResult(f"created issue {int(issue['id'])}: {issue['title']}")


def create_issue_tool(tracker_url: str, *, transport: httpx2.BaseTransport | None = None) -> Tool:
    """The tool, for the tracker at `tracker_url`. `transport` replaces the network in tests."""

    def client() -> httpx2.Client:
        return httpx2.Client(base_url=tracker_url, timeout=TIMEOUT, transport=transport)

    def run(call: ToolCall) -> ToolResult:
        title, body = call.input.get("title"), call.input.get("body")
        if not isinstance(title, str) or not title or not isinstance(body, str):
            return ToolResult(
                "input must have a non-empty string `title` and a string `body`", True
            )
        try:
            with client() as http:
                response = http.post(
                    "/issues",
                    json={"title": title, "body": body, "operation_id": call.operation_id},
                )
            if response.status_code != 201:
                return ToolResult(f"the tracker answered {response.status_code}", is_error=True)
            return _created(response.json())
        except httpx2.HTTPError as error:
            return ToolResult(f"the tracker could not be reached: {error!r}", is_error=True)
        except (ValueError, LookupError, TypeError) as error:
            return ToolResult(f"the tracker's answer was not an issue: {error!r}", is_error=True)

    def check(call: ToolCall) -> CheckAnswer:
        """Ask the tracker for an issue with this call's operation id.

        Found: done, with the result the run would have returned. A clear
        answer without such an issue: not done. This tracker shows an issue
        as soon as it has created it, which is what makes that answer safe.
        Anything else is unknown, and a person is asked.
        """
        try:
            with client() as http:
                response = http.get("/issues", params={"operation_id": call.operation_id})
            if response.status_code != 200:
                return CheckOutcome.UNKNOWN
            found = response.json()
            if not isinstance(found, list):
                return CheckOutcome.UNKNOWN
            if not found:
                return CheckOutcome.NOT_DONE
            return _created(found[0])
        except httpx2.HTTPError, ValueError, LookupError, TypeError:
            return CheckOutcome.UNKNOWN

    return Tool(
        name="create_issue",
        description="Open an issue in the issue tracker. Returns the number of the new issue.",
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "One line that names the problem"},
                "body": {"type": "string", "description": "The evidence, for example log lines"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        run=run,
        # Running it twice opens two issues.
        idempotent=False,
        check=check,
    )
