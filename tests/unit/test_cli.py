"""The `runtime` command against a stubbed API: arguments, requests, output, exit status."""

import io
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from runtime import cli
from runtime.api.models import JobSubmission
from runtime.cli import DEFAULT_API_URL, FOLLOW_ATTEMPTS, build_parser, format_step, main

JOB_ID = "01a1d655-88bd-7468-afc4-5ca3a1947906"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def job(status: str = "queued", error: str | None = None, tokens_used: int = 0) -> dict[str, Any]:
    return {
        "id": JOB_ID,
        "status": status,
        "spec": {"prompt": "go", "files": {}},
        "token_budget": 1000,
        "tokens_used": tokens_used,
        "deadline": "2026-11-13T20:00:00Z",
        "not_before": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "lease_epoch": 0,
        "cancel_requested": False,
        "error": error,
        "created_at": "2026-11-13T19:00:00Z",
    }


def step(seq: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "seq": seq,
        "kind": kind,
        "payload": payload,
        "lease_epoch": 1,
        "created_at": "2026-11-13T19:00:01Z",
    }


STEPS = [
    step(1, "model_request", {"model": "hello", "max_tokens": 100, "messages": 1}),
    step(
        2,
        "model_response",
        {
            "id": "msg_1",
            "content": [
                {"type": "text", "text": "Reading\nthe file."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.txt"}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 12, "output_tokens": 7},
        },
    ),
    step(3, "tool_intent", {"tool_use_id": "t1", "name": "read_file", "input": {"path": "a.txt"}}),
    step(4, "tool_result", {"tool_use_id": "t1", "content": "Hello!\n", "is_error": False}),
]
LINES = [
    "   1  model_request   model=hello messages=1",
    "   2  model_response  stop_reason=tool_use tokens=12+7 Reading the file.",
    '   3  tool_intent     read_file {"path": "a.txt"}',
    "   4  tool_result     ok Hello!",
]


def sse(steps: list[dict[str, Any]], end: str | None) -> httpx2.Response:
    text = "".join(f"id: {s['seq']}\nevent: step\ndata: {json.dumps(s)}\n\n" for s in steps)
    text += ": keep-alive\n\n"
    if end is not None:
        text += f"event: end\ndata: {json.dumps({'status': end})}\n\n"
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=text)


class StubApi:
    """Answers like the API and records what it was asked."""

    def __init__(self, final: dict[str, Any] | None = None, end: str | None = "succeeded") -> None:
        self.requests: list[httpx2.Request] = []
        self.final = final or job("succeeded", tokens_used=19)
        self.end = end
        self.submit_response = httpx2.Response(201, json=job())
        self.drop_after: int | None = None
        self.approve_response = httpx2.Response(200, json=job("queued"))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/jobs":
            return self.submit_response
        if path == f"/jobs/{JOB_ID}":
            return httpx2.Response(200, json=self.final)
        if request.method == "POST" and path == f"/jobs/{JOB_ID}/approve":
            return self.approve_response
        if path == f"/jobs/{JOB_ID}/steps":
            # Pages of two, to show that the command reads on.
            after = int(request.url.params.get("after", "0"))
            page = [s for s in STEPS if s["seq"] > after][:2]
            return httpx2.Response(200, json={"steps": page})
        if path == f"/jobs/{JOB_ID}/events":
            after = int(request.url.params.get("after", "0"))
            remaining = [s for s in STEPS if s["seq"] > after]
            # `drop_after` cuts each answer short, like a connection that breaks.
            return sse(remaining[: self.drop_after], self.end if self.drop_after is None else None)
        return httpx2.Response(404, json={"detail": "job not found"})


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests do not wait between two tries to follow a stream."""
    monkeypatch.setattr(cli, "FOLLOW_RETRY_DELAY", 0.0)


def run(*argv: str, api: StubApi | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    transport = httpx2.MockTransport(api or StubApi())
    code = main(list(argv), out=out, err=err, transport=transport)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def job_file(tmp_path: Path) -> Path:
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"prompt": "go", "files": {"a.txt": "Hello!\n"}}))
    return path


# Arguments


def test_arguments_are_parsed() -> None:
    parser = build_parser()
    submit = parser.parse_args(["submit", "job.json", "--follow"])
    assert (submit.command, submit.file, submit.follow) == ("submit", Path("job.json"), True)
    assert parser.parse_args(["submit", "job.json"]).follow is False
    status = parser.parse_args(["status", JOB_ID, "--json"])
    assert (status.command, status.job_id, status.json) == ("status", JOB_ID, True)
    logs = parser.parse_args(["logs", JOB_ID])
    assert (logs.command, logs.job_id, logs.follow) == ("logs", JOB_ID, False)


def test_api_url_comes_from_the_flag_then_the_environment_then_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNTIME_API_URL", raising=False)
    assert build_parser().parse_args(["status", JOB_ID]).api_url == DEFAULT_API_URL
    monkeypatch.setenv("RUNTIME_API_URL", "http://env:1")
    assert build_parser().parse_args(["status", JOB_ID]).api_url == "http://env:1"
    parsed = build_parser().parse_args(["--api-url", "http://flag:2", "status", JOB_ID])
    assert parsed.api_url == "http://flag:2"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["submit"],
        ["status"],
        ["logs"],
        ["dance", "x"],
        # A job id goes into the URL. Only a UUID is accepted.
        ["status", "../healthz"],
        ["logs", "not-a-uuid", "--follow"],
    ],
)
def test_bad_usage_exits_with_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(argv)
    assert stopped.value.code == 2
    assert "usage: runtime" in capsys.readouterr().err


# submit


def test_submit_posts_the_file_and_prints_the_job_id(job_file: Path) -> None:
    api = StubApi()
    code, out, err = run("submit", str(job_file), api=api)
    assert (code, out, err) == (0, f"{JOB_ID}\n", "")
    (request,) = api.requests
    assert (request.method, request.url.path) == ("POST", "/jobs")
    assert json.loads(request.content) == {"prompt": "go", "files": {"a.txt": "Hello!\n"}}


def test_submit_follow_prints_each_step_then_the_outcome(job_file: Path) -> None:
    code, out, err = run("submit", str(job_file), "--follow")
    assert out.splitlines() == [JOB_ID, *LINES, "succeeded  tokens_used=19"]
    assert (code, err) == (0, "")


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_following_a_job_that_did_not_succeed_exits_with_1(job_file: Path, status: str) -> None:
    api = StubApi(final=job(status, error="model call failed", tokens_used=3), end=status)
    code, out, _ = run("submit", str(job_file), "--follow", api=api)
    assert code == 1
    assert out.splitlines()[-1] == f"{status}  tokens_used=3  error=model call failed"


def test_submit_of_a_missing_or_broken_file_exits_with_2(tmp_path: Path) -> None:
    api = StubApi()
    code, out, err = run("submit", str(tmp_path / "nope.json"), api=api)
    assert (code, out) == (2, "")
    assert "cannot read" in err
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    code, _, err = run("submit", str(broken), api=api)
    assert code == 2
    assert "not valid JSON" in err
    assert api.requests == []


def test_a_submission_the_api_refuses_exits_with_2_and_shows_why(job_file: Path) -> None:
    api = StubApi()
    api.submit_response = httpx2.Response(
        422, json={"detail": [{"loc": ["body", "prompt"], "msg": "Field required"}]}
    )
    code, out, err = run("submit", str(job_file), api=api)
    assert (code, out) == (2, "")
    assert "422" in err
    assert "Field required" in err


# status


def test_status_prints_the_fields_that_are_set() -> None:
    code, out, _ = run("status", JOB_ID, api=StubApi(final=job("failed", error="boom")))
    assert code == 0
    assert out.splitlines() == [
        f"id:           {JOB_ID}",
        "status:       failed",
        "tokens_used:  0",
        "token_budget: 1000",
        "deadline:     2026-11-13T20:00:00Z",
        "created_at:   2026-11-13T19:00:00Z",
        "error:        boom",
    ]
    _, ok, _ = run("status", JOB_ID)
    assert "error:" not in ok


def test_status_json_prints_the_job_as_the_api_returned_it() -> None:
    code, out, _ = run("status", JOB_ID, "--json")
    assert code == 0
    assert json.loads(out) == job("succeeded", tokens_used=19)


@pytest.mark.parametrize("argv", [["status"], ["logs"], ["logs", "--follow"]])
def test_an_unknown_job_exits_with_2(argv: list[str]) -> None:
    command, *flags = argv
    code, out, err = run(command, "00000000-0000-7000-8000-000000000000", *flags)
    assert (code, out, err) == (2, "", "runtime: job not found\n")


# logs


def test_logs_prints_every_step_across_pages() -> None:
    api = StubApi()
    code, out, _ = run("logs", JOB_ID, api=api)
    assert code == 0
    assert out.splitlines() == LINES
    pages = [r.url.params["after"] for r in api.requests if r.url.path.endswith("/steps")]
    assert pages == ["0", "2", "4"]


def test_logs_follow_prints_the_stream_and_the_outcome() -> None:
    code, out, _ = run("logs", JOB_ID, "--follow")
    assert code == 0
    assert out.splitlines() == [*LINES, "succeeded  tokens_used=19"]


def test_a_stream_that_keeps_ending_without_its_end_event_exits_with_2() -> None:
    api = StubApi(end=None)
    code, out, err = run("logs", JOB_ID, "--follow", api=api)
    assert code == 2
    # Every step once: the tries after the first ask only for what is new.
    assert out.splitlines() == LINES
    assert "ended before the job did" in err
    asked = [r.url.params["after"] for r in api.requests if r.url.path.endswith("/events")]
    assert asked == ["0"] + ["4"] * FOLLOW_ATTEMPTS


def test_a_stream_that_breaks_is_continued_after_the_last_printed_step() -> None:
    """Each connection delivers one step and drops. The last one delivers the end."""

    class Dropping(StubApi):
        def __call__(self, request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/events"):
                after = int(request.url.params.get("after", "0"))
                self.drop_after = 1 if after < len(STEPS) else None
            return super().__call__(request)

    api = Dropping()
    code, out, err = run("logs", JOB_ID, "--follow", api=api)
    assert (code, err) == (0, "")
    assert out.splitlines() == [*LINES, "succeeded  tokens_used=19"]
    asked = [r.url.params["after"] for r in api.requests if r.url.path.endswith("/events")]
    assert asked == ["0", "1", "2", "3", "4"]


def test_a_connection_that_fails_while_following_is_tried_again_then_given_up() -> None:
    calls = 0

    def refuse(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise httpx2.ReadError("connection reset", request=request)

    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["logs", JOB_ID, "--follow"], out=out, err=err, transport=httpx2.MockTransport(refuse)
    )
    assert code == 2
    assert calls == FOLLOW_ATTEMPTS
    assert "the event stream broke" in err.getvalue()


def test_a_reader_that_goes_away_is_not_an_error() -> None:
    """`runtime logs <id> | head`: writing fails once the reader has closed the pipe."""

    class ClosedAfterOneLine(io.StringIO):
        def write(self, text: str) -> int:
            if self.getvalue().count("\n") >= 1:
                raise BrokenPipeError
            return super().write(text)

    out, err = ClosedAfterOneLine(), io.StringIO()
    code = main(["logs", JOB_ID], out=out, err=err, transport=httpx2.MockTransport(StubApi()))
    assert (code, err.getvalue()) == (0, "")
    assert out.getvalue().splitlines() == LINES[:1]


def test_an_api_that_cannot_be_reached_exits_with_2() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    out, err = io.StringIO(), io.StringIO()
    code = main(["status", JOB_ID], out=out, err=err, transport=httpx2.MockTransport(refuse))
    assert code == 2
    assert "cannot reach the API" in err.getvalue()


def test_a_step_with_unicode_line_separators_is_one_event() -> None:
    """U+2028, U+2029 and U+0085 are line ends to Python, not to an event stream."""
    text = "one\u2028two\u2029three\u0085four"
    tricky = step(1, "tool_result", {"tool_use_id": "t", "content": text, "is_error": False})
    body = (
        f"id: 1\nevent: step\ndata: {json.dumps(tricky, ensure_ascii=False)}\n\n"
        'event: end\ndata: {"status": "succeeded"}\n\n'
    )

    def api(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/events"):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        return httpx2.Response(200, json=job("succeeded"))

    out, err = io.StringIO(), io.StringIO()
    code = main(["logs", JOB_ID, "--follow"], out=out, err=err, transport=httpx2.MockTransport(api))
    assert (code, err.getvalue()) == (0, "")
    first = out.getvalue().split("\n")[0]
    assert first.startswith("   1  tool_result     ok one")
    assert "four" in first


@pytest.mark.parametrize(
    "events",
    [
        "event: step\ndata: not json\n\n",
        'event: step\ndata: {"seq": 1}\n\n',
        "event: end\ndata: {}\n\n",
        'event: end\ndata: ["succeeded"]\n\n',
    ],
)
def test_a_stream_with_unexpected_data_exits_with_2_without_a_traceback(events: str) -> None:
    def api(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, text=events)

    out, err = io.StringIO(), io.StringIO()
    code = main(["logs", JOB_ID, "--follow"], out=out, err=err, transport=httpx2.MockTransport(api))
    assert code == 2
    assert err.getvalue().startswith("runtime: unexpected data")
    assert "Traceback" not in err.getvalue()


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(200, text="<html>not the API</html>"),
        httpx2.Response(200, json=["a", "list"]),
        httpx2.Response(200, json={"status": "ok"}),
        httpx2.Response(302, headers={"location": "http://elsewhere/"}),
    ],
    ids=["html", "list", "other-object", "redirect"],
)
def test_an_answer_that_is_not_a_job_exits_with_2(response: httpx2.Response) -> None:
    out, err = io.StringIO(), io.StringIO()
    transport = httpx2.MockTransport(lambda request: response)
    code = main(["status", JOB_ID], out=out, err=err, transport=transport)
    assert code == 2
    assert err.getvalue().startswith("runtime: ")


@pytest.mark.parametrize("text", ['{"prompt": NaN}', '{"prompt": "\\ud800"}'])
def test_a_job_file_that_cannot_be_sent_exits_with_2(tmp_path: Path, text: str) -> None:
    path = tmp_path / "job.json"
    path.write_text(text)
    api = StubApi()
    code, out, err = run("submit", str(path), api=api)
    assert (code, out) == (2, "")
    assert err.startswith("runtime: ")


def test_an_api_url_that_is_not_a_url_exits_with_2() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--api-url", "http://[::1", "status", JOB_ID], out=out, err=err)
    assert code == 2
    assert "cannot reach the API" in err.getvalue()


# Step lines


def test_terminal_control_codes_are_not_printed() -> None:
    hostile = "\x1b[2K\x1b[1Agone\x1b]0;title\x07 done"
    line = format_step(
        step(4, "tool_result", {"tool_use_id": "t", "content": hostile, "is_error": False})
    )
    assert "\x1b" not in line
    assert "\x07" not in line
    assert line == "   4  tool_result     ok ?[2K?[1Agone?]0;title? done"


HOSTILE = "\x1b[2Kgone\x1b]0;title\x07\x9b1A\nnext"


def test_status_does_not_print_terminal_control_codes_from_the_error() -> None:
    # A job's error can quote model or tool text. It is shown whole, on one line.
    error = f"tool failed: {HOSTILE} " + "x" * 300
    code, out, _ = run("status", JOB_ID, api=StubApi(final=job("failed", error=error)))
    assert code == 0
    assert not {"\x1b", "\x07", "\x9b"} & set(out)
    assert out.splitlines()[-1] == (
        "error:        tool failed: ?[2Kgone?]0;title??1A next " + "x" * 300
    )


def test_a_refusal_that_is_not_json_is_shown_without_terminal_control_codes(
    job_file: Path,
) -> None:
    api = StubApi()
    api.submit_response = httpx2.Response(502, text=f"bad gateway {HOSTILE}")
    code, out, err = run("submit", str(job_file), api=api)
    assert (code, out) == (2, "")
    assert err == "runtime: the API answered 502: bad gateway ?[2Kgone?]0;title??1A next\n"


def test_error_and_failed_tool_steps_are_marked() -> None:
    failed = step(
        4, "tool_result", {"tool_use_id": "t", "content": "no such file", "is_error": True}
    )
    assert format_step(failed) == "   4  tool_result     error no such file"
    error = step(5, "error", {"message": "model call failed: 404"})
    assert format_step(error) == "   5  error           model call failed: 404"


def test_long_text_is_cut_to_one_short_line() -> None:
    long = step(9, "tool_result", {"tool_use_id": "t", "content": "x" * 500, "is_error": False})
    line = format_step(long)
    assert "\n" not in line
    assert line.endswith("...")
    assert len(line) < 140


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "m", "content": "just a string", "stop_reason": "end_turn", "usage": {}},
        {"id": "m", "content": ["not a block"], "stop_reason": "x", "usage": None},
        {
            "id": "m",
            "content": [{"type": "text"}],
            "stop_reason": "x",
            "usage": {"input_tokens": 1},
        },
        {},
    ],
)
def test_a_step_with_an_unusual_payload_is_printed_as_json(payload: dict[str, Any]) -> None:
    """The worker stores a malformed response as it came. Its log must still print."""
    line = format_step(step(2, "model_response", payload))
    assert line.startswith("   2  model_response")
    assert "\n" not in line
    for kind in ("model_request", "tool_intent", "tool_result", "error"):
        assert format_step(step(3, kind, {"odd": True})).endswith('{"odd": true}')


def test_an_unknown_step_kind_is_still_printed() -> None:
    assert format_step(step(7, "approval_requested", {"why": "rerun"})).startswith(
        "   7  approval_requested"
    )


# The example file


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.name)
def test_every_example_is_a_valid_submission(path: Path) -> None:
    JobSubmission.model_validate_json(path.read_text())


def test_there_is_a_hello_example() -> None:
    assert (EXAMPLES / "hello.json").is_file()


# approve


def test_approve_posts_to_the_job_and_prints_its_new_status() -> None:
    api = StubApi()
    code, out, err = run("approve", JOB_ID, api=api)
    assert (code, err) == (0, "")
    assert out == "queued\n"
    assert [(r.method, r.url.path) for r in api.requests] == [("POST", f"/jobs/{JOB_ID}/approve")]


def test_an_approval_the_api_refuses_exits_with_2_and_shows_why() -> None:
    api = StubApi()
    api.approve_response = httpx2.Response(
        409, json={"detail": "job is running; only a job that needs approval can be approved"}
    )
    code, out, err = run("approve", JOB_ID, api=api)
    assert (code, out) == (2, "")
    assert "409" in err
    assert "only a job that needs approval" in err


def test_approve_needs_a_job_id(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        run("approve", "not-a-job-id")
    assert stopped.value.code == 2
    assert "not a job id" in capsys.readouterr().err


def test_approval_steps_show_the_call_and_the_reason() -> None:
    asked = step(
        5,
        "approval_requested",
        {
            "tool_use_id": "t1",
            "name": "create_issue",
            "input": {"title": "CI is red"},
            "operation_id": "0190:t1",
            "reason": "the tool is not idempotent and has no check",
        },
    )
    given = step(6, "approval_given", {"tool_use_id": "t1", "operation_id": "0190:t1"})
    assert format_step(asked) == (
        "   5  approval_requested  create_issue 0190:t1:"
        " the tool is not idempotent and has no check"
    )
    assert format_step(given) == "   6  approval_given  0190:t1"


def test_a_tool_name_or_model_with_control_codes_is_not_printed_raw() -> None:
    """Both come from outside: the model names the tool, the submitter names the model."""
    intent = step(3, "tool_intent", {"tool_use_id": "t", "name": "read\x1b[2Jfile", "input": {}})
    request = step(1, "model_request", {"model": "m\x1b]0;x\x07", "max_tokens": 1, "messages": 1})
    for line in (format_step(intent), format_step(request)):
        assert "\x1b" not in line
        assert "\x07" not in line
