"""The `runtime` command: submit a job, read its status, read its steps.

It talks to the API over HTTP. The API's address comes from --api-url or
RUNTIME_API_URL and defaults to http://127.0.0.1:8000.

Exit status: 0 on success, 1 when a followed job did not succeed, 2 when
the command could not be carried out.
"""

import argparse
import json
import os
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, TextIO

import httpx2

DEFAULT_API_URL = "http://127.0.0.1:8000"
# Longest piece of a step shown on its line.
PREVIEW = 100


class CommandError(Exception):
    """The command cannot be carried out. The message is for the user."""


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
        "--follow", action="store_true", help="print the steps as they happen and wait for the end"
    )

    status = commands.add_parser("status", help="show a job's status")
    status.add_argument("job_id")
    status.add_argument("--json", action="store_true", help="print the job as JSON")

    logs = commands.add_parser("logs", help="print a job's steps")
    logs.add_argument("job_id")
    logs.add_argument(
        "--follow", action="store_true", help="keep printing new steps until the job ends"
    )
    return parser


def _preview(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= PREVIEW else line[: PREVIEW - 3] + "..."


def format_step(step: dict[str, Any]) -> str:
    """One line for one step: seq, kind, and the part of the payload worth a glance."""
    payload = step["payload"]
    kind = step["kind"]
    if kind == "model_request":
        detail = f"model={payload['model']} messages={payload['messages']}"
    elif kind == "model_response":
        usage = payload["usage"]
        text = " ".join(b["text"] for b in payload["content"] if b.get("type") == "text")
        detail = (
            f"stop_reason={payload['stop_reason']}"
            f" tokens={usage['input_tokens']}+{usage['output_tokens']} {_preview(text)}"
        )
    elif kind == "tool_intent":
        detail = f"{payload['name']} {_preview(json.dumps(payload['input']))}"
    elif kind == "tool_result":
        flag = "error" if payload["is_error"] else "ok"
        detail = f"{flag} {_preview(payload['content'])}"
    elif kind == "error":
        detail = _preview(payload["message"])
    else:
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
        raise CommandError(f"the API answered {response.status_code}: {detail}")
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


def _follow(client: httpx2.Client, job_id: str, out: TextIO) -> str:
    """Print steps from the event stream as they arrive. Returns the final status."""
    after = 0
    # timeout=None on reads: a job can be quiet for a long time between steps.
    timeout = httpx2.Timeout(10.0, read=None)
    with client.stream(
        "GET", f"/jobs/{job_id}/events", params={"after": after}, timeout=timeout
    ) as response:
        if response.status_code >= 400:
            response.read()
            _check(response)
        event, data = "", ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
            elif not line and event:
                if event == "step":
                    print(format_step(json.loads(data)), file=out, flush=True)
                elif event == "end":
                    status: str = json.loads(data)["status"]
                    return status
                event, data = "", ""
    raise CommandError("the event stream ended before the job did")


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
    """Print the final status line and turn the status into an exit code."""
    job = _job(client, job_id)
    line = f"{status}  tokens_used={job['tokens_used']}"
    if job["error"]:
        line += f"  error={_preview(job['error'])}"
    print(line, file=out)
    return 0 if status == "succeeded" else 1


def _status(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    job = _job(client, args.job_id)
    if args.json:
        print(json.dumps(job, indent=2), file=out)
        return 0
    for field in ("id", "status", "tokens_used", "token_budget", "deadline", "created_at", "error"):
        if job[field] is not None:
            print(f"{field + ':':<14}{job[field]}", file=out)
    return 0


def _logs(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    if args.follow:
        return _finish(_follow(client, args.job_id, out), client, args.job_id, out)
    _job(client, args.job_id)
    for step in _steps(client, args.job_id):
        print(format_step(step), file=out)
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
    handler = {"submit": _submit, "status": _status, "logs": _logs}[args.command]
    try:
        with httpx2.Client(base_url=args.api_url, timeout=30.0, transport=transport) as client:
            return handler(client, args, out)
    except CommandError as error:
        print(f"runtime: {error}", file=err)
    except httpx2.HTTPError as error:
        print(f"runtime: cannot reach the API at {args.api_url}: {error}", file=err)
    return 2


def run() -> None:
    """Console script entry point."""
    sys.exit(main())
