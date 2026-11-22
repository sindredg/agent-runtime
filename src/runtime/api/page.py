"""The status page: the job list as one HTML page that changes nothing.

It is the only HTML the API serves, at the API's own origin. A script that
ran there would pass the Origin check and could approve a job. So the page
has no script and no form, every value is escaped, and the
Content-Security-Policy lets the browser run nothing but the one style
below. Approve, deny and cancel stay in the CLI and the API.

Text that a model, a tool or a target wrote (a question's tool and reason,
a job's error) goes through `one_line` and then `html.escape`. Links are
built from a job id, a status and a number only, never from text.
"""

import base64
import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from html import escape
from urllib.parse import urlencode
from uuid import UUID

from runtime.store.listing import JobSummary
from runtime.store.status import JobStatus
from runtime.text import one_line

# How often the browser loads the page again. The refresh tag names no URL,
# so the filter and the page stay as they are.
REFRESH_SECONDS = 5

_STYLE = (
    "body{font-family:system-ui,sans-serif;margin:1rem}"
    "div.jobs{overflow-x:auto}"
    "table{border-collapse:collapse;width:100%}"
    "th,td{text-align:left;vertical-align:top;padding:.25rem .5rem;"
    "border-bottom:1px solid GrayText;white-space:nowrap}"
    "td.text{white-space:normal;min-width:12rem;overflow-wrap:anywhere}"
    "td.time{white-space:normal;min-width:6rem}"
    "td span{white-space:nowrap}"
    "nav a{margin-right:.5rem}"
    "a[aria-current]{font-weight:bold}"
)
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()

# The browser loads nothing, runs no script and sends no form. The last
# three directives do not fall back to `default-src`, so each is named.
CONTENT_SECURITY_POLICY = (
    f"default-src 'none'; style-src 'sha256-{_STYLE_HASH}';"
    " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = {
    "content-security-policy": CONTENT_SECURITY_POLICY,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


def _text(value: str) -> str:
    return escape(one_line(value), quote=True)


def _time(value: datetime) -> str:
    # Date and time each stay whole; the cell may put them on two lines.
    day, clock = value.astimezone(UTC).strftime("%Y-%m-%d|%H:%M:%S UTC").split("|")
    return f"<span>{escape(day, quote=True)}</span> <span>{escape(clock, quote=True)}</span>"


def _href(status: JobStatus | None, before: UUID | None = None, limit: int | None = None) -> str:
    """A link to a page of the list, from values that are never free text."""
    params: dict[str, str] = {}
    if status is not None:
        params["status"] = JobStatus(status).value
    if before is not None:
        params["before"] = str(UUID(str(before)))
    if limit is not None:
        params["limit"] = str(int(limit))
    return escape("/?" + urlencode(params) if params else "/", quote=True)


def _question(job: JobSummary) -> str:
    if job.question is not None:
        asked = job.question
        tool = _text(asked.tool) if asked.tool is not None else "unknown tool"
        reason = _text(asked.reason) if asked.reason is not None else "no reason given"
        return f"{int(asked.seq)}: {tool}: {reason}"
    if job.status is JobStatus.NEEDS_APPROVAL:
        return "not in the log yet"
    return ""


def _row(job: JobSummary) -> str:
    job_id = UUID(str(job.id))
    cells = [
        f'<td><a href="/jobs/{job_id}/steps">{job_id}</a></td>',
        f"<td>{escape(JobStatus(job.status).value, quote=True)}</td>",
        f'<td class="time">{_time(job.created_at)}</td>',
        f"<td>{int(job.tokens_used)} / {int(job.token_budget)}</td>",
        f'<td class="time">{_time(job.deadline)}</td>',
        f'<td class="text">{_question(job)}</td>',
        f'<td class="text">{_text(job.error) if job.error is not None else ""}</td>',
    ]
    return "<tr>" + "".join(cells) + "</tr>"


def render(
    jobs: Sequence[JobSummary], *, status: JobStatus | None, before: UUID | None, limit: int
) -> str:
    """The page for one page of the list, as `GET /jobs` with the same parameters returns it."""
    filters = [("all", None), *((s.value, s) for s in JobStatus)]
    nav = " ".join(
        f'<a href="{_href(value)}"'
        + (' aria-current="page"' if value == status else "")
        + f">{escape(name, quote=True)}</a>"
        for name, value in filters
    )
    if jobs:
        body = (
            '<div class="jobs"><table><thead><tr>'
            "<th>Job (its steps)</th><th>Status</th><th>Created</th>"
            "<th>Tokens used / budget</th><th>Deadline</th><th>Question</th><th>Error</th>"
            "</tr></thead><tbody>" + "".join(_row(job) for job in jobs) + "</tbody></table></div>"
        )
    else:
        body = "<p>No jobs.</p>"
    more = ""
    if len(jobs) == limit:
        older = _href(status, before=jobs[-1].id, limit=limit)
        more = f'<p><a href="{older}">Older jobs</a></p>'
    newest = ""
    if before is not None:
        newest = f'<p><a href="{_href(status, limit=limit)}">Newest jobs</a></p>'
    return (
        "<!doctype html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        "<title>agent-runtime jobs</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        "<h1>Jobs</h1>"
        f"<p>Newest first. Loads again every {REFRESH_SECONDS} s. This page changes nothing:"
        " approve, deny and cancel with the CLI or the API.</p>"
        f"<nav>Status: {nav}</nav>"
        f"{newest}{body}{more}"
        "</body></html>"
    )
