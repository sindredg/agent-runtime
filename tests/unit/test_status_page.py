"""The status page (`GET /`) lists the jobs and changes nothing.

Text a model, a tool or a target wrote reaches the page in a question's tool
and reason and in a job's error. The page is parsed as a browser would, and
only known tags, attributes and links may come out of it.
"""

import base64
import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from uuid import UUID, uuid7

import pytest
from fastapi.testclient import TestClient

from runtime.api import page
from runtime.api.app import create_app
from runtime.config import ApiConfig
from runtime.store.listing import JobSummary, Question
from runtime.store.status import JobStatus
from runtime.text import one_line

HOSTILE = [
    "<script>alert(1)</script>",
    '" onmouseover="alert(1)',
    "' autofocus onfocus='alert(1)",
    "</style><script>alert(1)</script>",
    "</td></tr></table><form method=post action=/jobs/x/approve><button>go</button></form>",
    "<a href=javascript:alert(1)>x</a> &amp; &lt;",
    "‮evil\x1b[31m\r\nline",
]

TAGS = {
    "html": {"lang"},
    "head": set(),
    "meta": {"charset", "name", "content", "http-equiv"},
    "title": set(),
    "style": set(),
    "body": set(),
    "h1": set(),
    "p": set(),
    "nav": set(),
    "a": {"href", "aria-current"},
    "table": set(),
    "thead": set(),
    "tbody": set(),
    "tr": set(),
    "th": set(),
    "td": {"class"},
    "div": {"class"},
    "span": set(),
}
_ID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_STATUS = "|".join(s.value for s in JobStatus)
HREF = re.compile(
    rf"/|/jobs/{_ID}/steps|/\?(status=({_STATUS}))?(&?before={_ID})?(&?limit=[0-9]+)?"
)


class Parsed(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.style: list[str] = []
        self._in_style = False
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_style = tag == "style"

    def handle_endtag(self, tag: str) -> None:
        self._in_style = False

    def handle_data(self, data: str) -> None:
        (self.style if self._in_style else self.text).append(data)

    def hrefs(self) -> list[str]:
        return [str(attrs["href"]) for tag, attrs in self.tags if tag == "a"]


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def summary(status: JobStatus, **fields: object) -> JobSummary:
    job = JobSummary(
        id=uuid7(),
        status=status,
        token_budget=1000,
        tokens_used=12,
        deadline=NOW + timedelta(hours=1),
        error=None,
        created_at=NOW,
        question=None,
    )
    return replace(job, **fields)  # type: ignore[arg-type]


def every_status(text: str) -> list[JobSummary]:
    """A job in each status the code knows, with `text` wherever a job carries text."""
    jobs = []
    for status in JobStatus:
        question = None
        if status is JobStatus.NEEDS_APPROVAL:
            question = Question(seq=7, tool=text, reason=text)
        jobs.append(summary(status, error=text, question=question))
    # A waiting job whose question has no name or reason, and one whose
    # question is not in the log yet.
    jobs.append(summary(JobStatus.NEEDS_APPROVAL, question=Question(3, None, None)))
    jobs.append(summary(JobStatus.NEEDS_APPROVAL))
    return jobs


def render(jobs: list[JobSummary], **options: object) -> Parsed:
    params: dict[str, object] = {"status": None, "before": None, "limit": 50, **options}
    return Parsed(page.render(jobs, **params))  # type: ignore[arg-type]


def test_every_status_renders_with_its_job_and_link_to_its_steps() -> None:
    jobs = every_status("boom")
    parsed = render(jobs)

    cells = [text.strip() for text in parsed.text]
    for job in jobs:
        assert f"/jobs/{job.id}/steps" in parsed.hrefs()
    # Each status appears as a row's cell, besides its filter link.
    assert {s.value for s in JobStatus} <= set(cells)
    text = "".join(parsed.text)
    assert "7: boom: boom" in text
    assert "3: unknown tool: no reason given" in text
    assert "not in the log yet" in text


@pytest.mark.parametrize("hostile", HOSTILE)
def test_text_from_a_job_comes_out_as_text_and_never_as_markup(hostile: str) -> None:
    parsed = render(every_status(hostile))

    for tag, attrs in parsed.tags:
        assert tag in TAGS, tag
        assert set(attrs) <= TAGS[tag], (tag, attrs)
    for href in parsed.hrefs():
        assert HREF.fullmatch(href), href
    assert parsed.style == [page._STYLE]
    # Shown as it was written, with control codes as `?`: once in the
    # question's tool, once in its reason, and once in each job's error.
    shown = one_line(hostile)
    assert "".join(parsed.text).count(shown) == len(JobStatus) + 2


@pytest.mark.parametrize("hostile", HOSTILE)
def test_the_page_has_no_form_and_no_script(hostile: str) -> None:
    parsed = render(every_status(hostile))

    found = {tag for tag, _ in parsed.tags}
    assert not found & {"form", "input", "button", "select", "textarea", "script", "iframe"}


def test_the_style_on_the_page_is_the_one_the_policy_allows() -> None:
    parsed = render(every_status("x"))

    (style,) = parsed.style
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    # The whole policy, written out: a source added to it must fail here.
    assert (
        f"default-src 'none'; style-src 'sha256-{digest}';"
        " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ) == page.CONTENT_SECURITY_POLICY


def test_no_jobs_says_so() -> None:
    parsed = render([])

    assert "No jobs." in "".join(parsed.text)
    assert "table" not in {tag for tag, _ in parsed.tags}


def test_a_full_page_links_to_the_older_jobs_with_the_same_filter() -> None:
    jobs = [summary(JobStatus.QUEUED) for _ in range(3)]

    full = render(jobs, status=JobStatus.QUEUED, limit=3)
    short = render(jobs, status=JobStatus.QUEUED, limit=4)

    assert f"/?status=queued&before={jobs[-1].id}&limit=3" in full.hrefs()
    assert not [href for href in short.hrefs() if "before=" in href]


def test_an_older_page_links_back_to_the_newest_and_marks_its_filter() -> None:
    before = UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")
    parsed = render([], status=JobStatus.FAILED, before=before, limit=10)

    assert "/?status=failed&limit=10" in parsed.hrefs()
    current = [attrs["href"] for tag, attrs in parsed.tags if "aria-current" in attrs]
    assert current == ["/?status=failed"]


def test_the_page_reloads_itself_without_naming_a_url() -> None:
    parsed = render([])

    (refresh,) = [attrs for tag, attrs in parsed.tags if attrs.get("http-equiv") == "refresh"]
    assert refresh["content"] == str(page.REFRESH_SECONDS)


CONFIG = ApiConfig(
    database_url="postgresql+asyncpg://unused/unused",
    default_token_budget=1000,
    default_timeout_seconds=60,
    host="127.0.0.1",
    port=8000,
    allowed_hosts=("localhost", "127.0.0.1"),
)


def client(base_url: str = "http://127.0.0.1:8000") -> TestClient:
    # Not entered as a context manager: the lifespan, which connects to the
    # database, does not run. These requests never reach the list.
    return TestClient(create_app(CONFIG), base_url=base_url)


def test_the_page_is_refused_by_a_name_the_api_does_not_answer_to() -> None:
    assert client("http://rebound.example:8000").get("/").status_code == 400


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_page_takes_no_change(method: str) -> None:
    assert client().request(method, "/").status_code == 405
