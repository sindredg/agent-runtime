"""The `runtime` command: submit a job, read its status and steps, approve a rerun.

It talks to the API over HTTP. The API's address comes from --api-url or
RUNTIME_API_URL and defaults to http://127.0.0.1:8000.

Exit status: 0 on success, 1 when a followed job did not succeed, 2 when
the command could not be carried out, 3 when a followed job stopped to wait
for approval.
"""

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, TextIO
from uuid import UUID

import httpx2

DEFAULT_API_URL = "http://127.0.0.1:8000"
# Following a job: seconds without any data before a connection counts as
# dead, tries in a row without a new step before giving up, and the wait
# between two tries.
FOLLOW_READ_TIMEOUT = 60.0
FOLLOW_ATTEMPTS = 5
FOLLOW_RETRY_DELAY = 1.0
# Longest piece of a step shown on its line.
PREVIEW = 100
# Exit status when a followed job stopped to wait for approval.
EXIT_NEEDS_APPROVAL = 3


class CommandError(Exception):
    """The command cannot be carried out. The message is for the user."""


def _job_id(text: str) -> str:
    """A job id is a UUID. It goes into the URL, so anything else is refused here."""
    try:
        return str(UUID(text))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{text!r} is not a job id") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="runtime", description="Submit and inspect agent jobs.")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("RUNTIME_API_URL", DEFAULT_API_URL),
        help="address of the API (default: RUNTIME_API_URL or %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    submit = commands.add_parser("submit", help="submit a job from a JSON file")
    submit.add_argument("file", type=Path, help="JSON file with the job, see examples/")
    submit.add_argument(
        "--follow",
        action="store_true",
        help="print the steps as they happen, until the job ends or needs approval",
    )

    status = commands.add_parser("status", help="show a job's status")
    status.add_argument("job_id", type=_job_id)
    status.add_argument("--json", action="store_true", help="print the job as JSON")

    logs = commands.add_parser("logs", help="print a job's steps")
    logs.add_argument("job_id", type=_job_id)
    logs.add_argument(
        "--follow",
        action="store_true",
        help="keep printing new steps until the job ends or needs approval",
    )

    approve = commands.add_parser(
        "approve",
        help="let a job that needs approval run its interrupted tool call again",
        description=(
            "A job stops in needs_approval when a worker went away during a tool call and"
            " nobody can tell whether the call happened. Approving runs the call again."
            " Look at the system the tool changes first: the call may already have been made."
        ),
    )
    approve.add_argument("job_id", type=_job_id)
    return parser


def _one_line(text: str) -> str:
    """The text on one line. Text from a model, a tool or another server can
    hold terminal control codes; anything that is not printable is shown as `?`."""
    return "".join(char if char.isprintable() else "?" for char in " ".join(text.split()))


def _preview(text: str) -> str:
    """One short line: `_one_line`, cut to PREVIEW characters."""
    line = _one_line(text)
    return line if len(line) <= PREVIEW else line[: PREVIEW - 3] + "..."


def _detail(kind: str, payload: dict[str, Any]) -> str:
    if kind == "model_request":
        return f"model={_preview(str(payload['model']))} messages={payload['messages']}"
    if kind == "model_response":
        usage = payload["usage"]
        text = " ".join(b["text"] for b in payload["content"] if b.get("type") == "text")
        return (
            f"stop_reason={payload['stop_reason']}"
            f" tokens={usage['input_tokens']}+{usage['output_tokens']} {_preview(text)}"
        )
    if kind == "tool_intent":
        return f"{_preview(str(payload['name']))} {_preview(json.dumps(payload['input']))}"
    if kind == "tool_result":
        flag = "error" if payload["is_error"] else "ok"
        return f"{flag} {_preview(payload['content'])}"
    if kind == "error":
        return _preview(payload["message"])
    if kind == "approval_requested":
        return _preview(f"{payload['name']} {payload['operation_id']}: {payload['reason']}")
    if kind == "approval_given":
        return _preview(payload["operation_id"])
    return _preview(json.dumps(payload))


def format_step(step: dict[str, Any]) -> str:
    """One line for one step: seq, kind, and the part of the payload worth a glance.

    A payload that does not have the usual shape is shown as JSON. The worker
    stores a malformed model response as it came, and that job's log still
    has to be readable.
    """
    payload, kind = step["payload"], step["kind"]
    try:
        detail = _detail(kind, payload)
    except LookupError, TypeError, AttributeError:
        detail = _preview(json.dumps(payload))
    return f"{step['seq']:>4}  {kind:<14}  {detail}".rstrip()


def _check(response: httpx2.Response) -> httpx2.Response:
    if response.status_code == 404:
        raise CommandError("job not found")
    if response.status_code >= 400:
        try:
            detail = json.dumps(response.json().get("detail"))
        except ValueError, AttributeError:
            detail = response.text
        raise CommandError(f"the API answered {response.status_code}: {_one_line(detail)}")
    return response


def _job(client: httpx2.Client, job_id: str) -> dict[str, Any]:
    job: dict[str, Any] = _check(client.get(f"/jobs/{job_id}")).json()
    return job


def _steps(client: httpx2.Client, job_id: str) -> Iterator[dict[str, Any]]:
    """Every step the job has now, read page by page."""
    after = 0
    while True:
        page = _check(client.get(f"/jobs/{job_id}/steps", params={"after": after})).json()["steps"]
        if not page:
            return
        yield from page
        after = page[-1]["seq"]


def _lines(response: httpx2.Response) -> Iterator[str]:
    """The stream's lines, split at line feeds only.

    `iter_lines` would also split at U+2028 and other characters that are
    line ends to Python but ordinary data in an event stream.
    """
    buffer = ""
    for chunk in response.iter_text():
        buffer += chunk
        *complete, buffer = buffer.split("\n")
        for line in complete:
            yield line.removesuffix("\r")
    if buffer:
        yield buffer.removesuffix("\r")


def _follow(client: httpx2.Client, job_id: str, out: TextIO) -> str:
    """Print steps from the event stream as they arrive. Returns the status it stopped at.

    That is the job's final status, or `needs_approval`: such a job goes on
    only when a person approves it, so following it would wait without end.

    A stream that breaks is opened again after the last step that was
    printed. It is given up after FOLLOW_ATTEMPTS tries in a row that
    brought nothing new.
    """
    after = 0
    failures = 0
    # The API sends a comment line at least every 15 seconds. A read that
    # waits much longer than that is a dead connection.
    timeout = httpx2.Timeout(10.0, read=FOLLOW_READ_TIMEOUT)
    while True:
        printed = after
        try:
            with client.stream(
                "GET", f"/jobs/{job_id}/events", params={"after": after}, timeout=timeout
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    _check(response)
                event, data = "", ""
                for line in _lines(response):
                    if line.startswith("event: "):
                        event = line.removeprefix("event: ")
                    elif line.startswith("data: "):
                        data = line.removeprefix("data: ")
                    elif not line and event:
                        if event == "step":
                            step = json.loads(data)
                            print(format_step(step), file=out, flush=True)
                            after = step["seq"]
                        elif event == "end":
                            status: str = json.loads(data)["status"]
                            return status
                        elif event == "status" and json.loads(data)["status"] == "needs_approval":
                            return "needs_approval"
                        event, data = "", ""
            problem = "the event stream ended before the job did"
        except httpx2.TransportError as error:
            problem = f"the event stream broke: {error!r}"
        failures = 0 if after > printed else failures + 1
        if failures >= FOLLOW_ATTEMPTS:
            raise CommandError(problem)
        time.sleep(FOLLOW_RETRY_DELAY)


def _submit(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    try:
        submission = json.loads(args.file.read_text(encoding="utf-8"))
    except OSError as error:
        raise CommandError(f"cannot read {args.file}: {error.strerror}") from error
    except ValueError as error:
        raise CommandError(f"{args.file} is not valid JSON: {error}") from error
    job = _check(client.post("/jobs", json=submission)).json()
    print(job["id"], file=out, flush=True)
    if not args.follow:
        return 0
    return _finish(_follow(client, job["id"], out), client, job["id"], out)


def _finish(status: str, client: httpx2.Client, job_id: str, out: TextIO) -> int:
    """Print the line for the status the job stopped at and turn it into an exit code."""
    job = _job(client, job_id)
    line = f"{status}  tokens_used={job['tokens_used']}"
    if job["error"]:
        line += f"  error={_preview(job['error'])}"
    print(line, file=out)
    if status == "needs_approval":
        print(
            "The job waits for approval: a tool call was interrupted and may or may not have run.",
            file=out,
        )
        print(f"To run the call again: runtime approve {_one_line(job_id)}", file=out)
        return EXIT_NEEDS_APPROVAL
    return 0 if status == "succeeded" else 1


def _status(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    job = _job(client, args.job_id)
    if args.json:
        print(json.dumps(job, indent=2), file=out)
        return 0
    for field in ("id", "status", "tokens_used", "token_budget", "deadline", "created_at", "error"):
        if job[field] is not None:
            # The error is not cut: this is where it is read in full.
            print(f"{field + ':':<14}{_one_line(str(job[field]))}", file=out)
    return 0


def _logs(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    if args.follow:
        return _finish(_follow(client, args.job_id, out), client, args.job_id, out)
    _job(client, args.job_id)
    for step in _steps(client, args.job_id):
        print(format_step(step), file=out)
    return 0


def _approve(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    job = _check(client.post(f"/jobs/{args.job_id}/approve")).json()
    print(job["status"], file=out)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    transport: httpx2.BaseTransport | None = None,
) -> int:
    """Run the command. `transport` replaces the network in tests."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    handlers = {"submit": _submit, "status": _status, "logs": _logs, "approve": _approve}
    handler = handlers[args.command]
    try:
        with httpx2.Client(base_url=args.api_url, timeout=30.0, transport=transport) as client:
            return handler(client, args, out)
    except BrokenPipeError:
        # The reader went away, as in `runtime logs <id> | head`. Not an
        # error. Python would complain again when it flushes stdout at exit,
        # so stdout is pointed somewhere that accepts the rest.
        if out is sys.stdout:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except CommandError as error:
        print(f"runtime: {error}", file=err)
    except (httpx2.HTTPError, httpx2.InvalidURL) as error:
        print(f"runtime: cannot reach the API at {args.api_url}: {error}", file=err)
    except (ValueError, LookupError, TypeError, AttributeError) as error:
        # An answer that is not what the API sends, or a job file that cannot
        # be sent as JSON. Reported in one line; exit status 1 stays reserved
        # for a job that did not succeed.
        print(f"runtime: unexpected data: {type(error).__name__}: {error}", file=err)
    return 2


def run() -> None:
    """Console script entry point."""
    sys.exit(main())


if __name__ == "__main__":
    run()
