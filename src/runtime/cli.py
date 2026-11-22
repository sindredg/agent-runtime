"""The `runtime` command: submit a job, list jobs, read a job's status, steps
and answer, cancel it, and approve or deny a tool call that waits for a person.

It talks to the API over HTTP. The API's address comes from --api-url or
RUNTIME_API_URL and defaults to http://127.0.0.1:8000. `runtime verify` is
the exception: it reads the database itself (`runtime.verify`).

Exit status: 0 on success, 1 when a followed job did not succeed or a chain
does not hold, 2 when the command could not be carried out, 3 when a followed
job stopped to wait for approval.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NoReturn, TextIO
from uuid import UUID

import httpx2

from runtime.verify import DEFAULT_DATABASE_URL, verify

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
# Jobs in one page of `runtime list`: the most the API returns, and the default.
MAX_LIST = 200
DEFAULT_LIST = 50
# The statuses a job can have, as the API names them.
STATUSES = (
    "queued",
    "waiting",
    "needs_approval",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)


class CommandError(Exception):
    """The command cannot be carried out. The message is for the user."""


def _job_id(text: str) -> str:
    """A job id is a UUID. It goes into the URL, so anything else is refused here."""
    try:
        return str(UUID(text))
    except ValueError as error:
        # The text is not repeated: a URL with a password typed in the wrong
        # place would be printed whole.
        raise argparse.ArgumentTypeError("not a job id") from error


def _page_size(text: str) -> int:
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= MAX_LIST:
        raise argparse.ArgumentTypeError(f"not a number from 1 to {MAX_LIST}")
    return int(text)


def _question(text: str) -> int:
    """A question is the number (`seq`) of a job's approval_requested step."""
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= 2**31 - 1:
        raise argparse.ArgumentTypeError("not a question number")
    return int(text)


class _Parser(argparse.ArgumentParser):
    """argparse, without repeating the text it refused.

    A database URL typed in the wrong place would be printed whole, password
    included. The subcommands' parsers are of this class too.
    """

    def error(self, message: str) -> NoReturn:
        if message.startswith(("unrecognized arguments", "ambiguous option")):
            # These name the typed text without quotes.
            message = message.split(":", 1)[0]
        else:
            # The others quote it with repr(): in single quotes, or in double
            # quotes when it holds a single quote. Everything from the first
            # quote on goes, the list of choices too; usage shows them.
            message = re.sub(r"""['"].*""", "...", message, flags=re.DOTALL)
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="runtime", description="Submit and inspect agent jobs.")
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

    listing = commands.add_parser(
        "list",
        help="list jobs, newest first",
        description=(
            "One line per job: id, status, creation time, tokens used of the budget. A job"
            " that waits for approval shows its question under it. When the page is full,"
            " the id to continue from is printed on stderr."
        ),
    )
    listing.add_argument("--status", choices=STATUSES, help="only jobs in this status")
    listing.add_argument(
        "--limit",
        type=_page_size,
        default=DEFAULT_LIST,
        help=f"jobs in the page, 1 to {MAX_LIST} (default: %(default)s)",
    )
    listing.add_argument(
        "--before", type=_job_id, help="only jobs older than this job id: the next page"
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

    answer = commands.add_parser(
        "answer",
        help="print a succeeded job's final answer in full",
        description=(
            "Prints the text of the job's last model response, the answer it ended with,"
            " whole and with its line breaks. Characters a terminal would act on are shown"
            " as ?. A job that has not succeeded has no final answer: exit status 2."
        ),
    )
    answer.add_argument("job_id", type=_job_id)

    approve = commands.add_parser(
        "approve",
        help="let the tool call of a job's question run",
        description=(
            "A job stops in needs_approval for one of two reasons, which its question (its"
            " approval_requested step) names. The tool needs approval before each run:"
            " nothing of the call has run, and approving runs it once. Or a worker went away"
            " during the call and nobody can tell whether it happened: approving runs it"
            " again, so look at the system the tool changes first. The answer names the"
            " question by its number. If the job waits on another question by then, the"
            " answer is refused and nothing changes."
        ),
    )
    approve.add_argument("job_id", type=_job_id)
    approve.add_argument("question", type=_question, help="the number of the question")

    deny = commands.add_parser(
        "deny",
        help="refuse the tool call of a job's question",
        description=(
            "The call does not run. The job goes on, and the model is told that the call"
            " was denied. A job nobody answers is failed at its deadline. The answer names"
            " the question by its number, as for approve."
        ),
    )
    deny.add_argument("job_id", type=_job_id)
    deny.add_argument("question", type=_question, help="the number of the question")

    cancel = commands.add_parser(
        "cancel",
        help="cancel a job",
        description=(
            "A job that no worker holds is cancelled at once. A running job is stopped by its"
            " worker within a few seconds; a running tool command is killed. Prints the"
            " status the job has now: cancelled, or running while its worker stops it."
        ),
    )
    cancel.add_argument("job_id", type=_job_id)

    check = commands.add_parser(
        "verify",
        help="check that a job's step log is as it was written",
        description=(
            "Recomputes the hash chain of each job's steps and compares it with the stored"
            " hashes and the job's head. Reads the database directly, not the API, in a"
            " read-only transaction. Exit status 0: every chain holds. 1: a mismatch, named"
            " by job and step. 2: a job could not be checked."
        ),
    )
    which = check.add_mutually_exclusive_group(required=True)
    which.add_argument("job_ids", nargs="*", type=_job_id, default=[], metavar="job_id")
    which.add_argument("--all", action="store_true", help="check every job")
    check.add_argument(
        "--database-url",
        default=os.environ.get("RUNTIME_DATABASE_URL", DEFAULT_DATABASE_URL),
        help="the database, as the app role is enough (default: RUNTIME_DATABASE_URL"
        " or the stack's app role on 127.0.0.1)",
    )
    return parser


def _one_line(text: str) -> str:
    """The text on one line. Text from a model, a tool or another server can
    hold terminal control codes; anything that is not printable is shown as `?`."""
    return "".join(char if char.isprintable() else "?" for char in " ".join(text.split()))


def _text(text: str) -> str:
    """Text to print whole: line breaks and tabs stay, anything else that is
    not printable is shown as `?`. A carriage return is one of those: it
    would let the text write over what was printed before it."""
    return "".join(c if c.isprintable() or c in "\n\t" else "?" for c in text)


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
        # The reason first: it says whether the call has run before.
        return _preview(f"{payload['name']}: {payload['reason']} ({payload['operation_id']})")
    if kind in ("approval_given", "approval_denied"):
        return _preview(payload["operation_id"])
    if kind == "status_changed":
        change = f"{payload['from']} -> {payload['to']} ({payload['cause']})"
        if "not_before" in payload:
            change += f" until {payload['not_before']}"
        return _preview(change)
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


def _follow(client: httpx2.Client, job_id: str, out: TextIO) -> tuple[str, Any]:
    """Print steps from the event stream as they arrive. Returns the status it
    stopped at, and the last `approval_requested` step it printed.

    The status is the job's final one, or `needs_approval`: such a job goes
    on only when a person answers, so following it would wait without end.

    A stream that breaks is opened again after the last step that was
    printed. It is given up after FOLLOW_ATTEMPTS tries in a row that
    brought nothing new.
    """
    after = 0
    failures = 0
    asked: Any = None
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
                            if step["kind"] == "approval_requested":
                                asked = step
                        elif event == "end":
                            status: str = json.loads(data)["status"]
                            return status, asked
                        elif event == "status" and json.loads(data)["status"] == "needs_approval":
                            return "needs_approval", asked
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
    return _finish(*_follow(client, job["id"], out), client, job["id"], out)


def _open_question(client: httpx2.Client, job_id: str) -> dict[str, Any] | None:
    """The job's last `approval_requested` step: the question it waits on, if it waits."""
    asked = None
    for step in _steps(client, job_id):
        if step["kind"] == "approval_requested":
            asked = step
    return asked


def _how_to_run() -> str:
    """How to start `runtime` again from the shell this command was started in.

    `uv run` sets UV for the command it starts. After the quickstart the CLI
    runs that way and `runtime` alone is not on the PATH. Otherwise the
    command was found on the PATH, or in an activated environment.
    """
    return "uv run runtime" if os.environ.get("UV") else "runtime"


def _finish(status: str, asked: Any, client: httpx2.Client, job_id: str, out: TextIO) -> int:
    """Print the line for the status the job stopped at and turn it into an exit code.

    `asked` is the question the job waits on, as the stream showed it. If
    the stream did not show one, it is read from the job's steps.
    """
    job = _job(client, job_id)
    line = f"{status}  tokens_used={job['tokens_used']}"
    if job["error"]:
        line += f"  error={_preview(job['error'])}"
    print(line, file=out)
    if status == "needs_approval":
        if not isinstance(asked, dict):
            asked = _open_question(client, job_id)
        print("The job waits for a person to decide about a tool call.", file=out)
        if asked is None:
            again = f"run `{_how_to_run()} status` again"
            print(f"Its question is not in the log yet: {again}.", file=out)
            return EXIT_NEEDS_APPROVAL
        payload, number = asked["payload"], int(asked["seq"])
        # In full: this is what the person decides about. The input is
        # printed exactly, as JSON: that is one line of ASCII, with
        # everything a terminal would act on escaped.
        print(f"  question: {number}", file=out)
        print(f"  tool:     {_one_line(str(payload.get('name')))}", file=out)
        print(f"  input:    {json.dumps(payload.get('input'))}", file=out)
        print(f"  reason:   {_one_line(str(payload.get('reason')))}", file=out)
        command = _how_to_run()
        print(f"To let the call run: {command} approve {_one_line(job_id)} {number}", file=out)
        print(f"To refuse it:        {command} deny {_one_line(job_id)} {number}", file=out)
        return EXIT_NEEDS_APPROVAL
    return 0 if status == "succeeded" else 1


def _list(client: httpx2.Client, args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    params: dict[str, str | int] = {"limit": args.limit}
    if args.status is not None:
        params["status"] = args.status
    if args.before is not None:
        params["before"] = args.before
    found: list[dict[str, Any]] = _check(client.get("/jobs", params=params)).json()["jobs"]
    for job in found:
        fields = [_one_line(str(job[name])) for name in ("id", "status", "created_at")]
        tokens = f"tokens {int(job['tokens_used'])}/{int(job['token_budget'])}"
        print(f"{fields[0]}  {fields[1]:<14}  {fields[2]}  {tokens}", file=out)
        asked = job.get("question")
        if asked is not None:
            # A glance: the reason is cut. `runtime status` and `logs` read the job.
            text = _preview(f"{asked['tool']}: {asked['reason']}")
            print(f"    question {int(asked['seq'])}: {text}", file=out)
        elif job["status"] == "needs_approval":
            print("    question: not in the log yet", file=out)
    if len(found) == args.limit:
        # Not a command to copy: how `runtime` is started depends on the install.
        # A UUID or nothing: a user pastes this text into a shell.
        last = UUID(str(found[-1]["id"]))
        print(f"More jobs may be older. Add --before {last} to read them.", file=err)
    return 0


def _status(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    job = _job(client, args.job_id)
    if args.json:
        print(json.dumps(job, indent=2), file=out)
        return 0
    for field in ("id", "status", "tokens_used", "token_budget", "deadline", "created_at", "error"):
        if job[field] is not None:
            # The error is not cut: this is where it is read in full.
            print(f"{field + ':':<14}{_one_line(str(job[field]))}", file=out)
    if job["status"] == "needs_approval":
        asked = _open_question(client, args.job_id)
        if asked is not None:
            name = _one_line(str(asked["payload"].get("name")))
            print(f"{'question:':<14}{int(asked['seq'])} ({name})", file=out)
    return 0


def _logs(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    if args.follow:
        return _finish(*_follow(client, args.job_id, out), client, args.job_id, out)
    _job(client, args.job_id)
    for step in _steps(client, args.job_id):
        print(format_step(step), file=out)
    return 0


def _answer(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    """The text of the last model response. A job succeeds only on a response
    that ends its turn, so for a succeeded job that response is the answer."""
    status = _job(client, args.job_id)["status"]
    if status != "succeeded":
        raise CommandError(
            f"the job is {_one_line(str(status))}: only a succeeded job has an answer"
        )
    final = None
    for step in _steps(client, args.job_id):
        if step["kind"] == "model_response":
            final = step
    if final is None:
        raise CommandError("the job's log has no model response")
    blocks = final["payload"]["content"]
    text = "\n".join(
        str(b["text"]) for b in blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    if not text:
        raise CommandError("the job's last model response has no text")
    print(_text(text), file=out)
    return 0


def _approve(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    answer = {"question": args.question}
    job = _check(client.post(f"/jobs/{args.job_id}/approve", json=answer)).json()
    print(_one_line(str(job["status"])), file=out)
    return 0


def _deny(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    answer = {"question": args.question}
    job = _check(client.post(f"/jobs/{args.job_id}/deny", json=answer)).json()
    print(_one_line(str(job["status"])), file=out)
    return 0


def _cancel(client: httpx2.Client, args: argparse.Namespace, out: TextIO) -> int:
    response = _check(client.post(f"/jobs/{args.job_id}/cancel"))
    status = _one_line(str(response.json()["status"]))
    if response.status_code == 202:
        print(f"{status}; cancel requested, its worker stops it", file=out)
    else:
        print(status, file=out)
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
    if args.command == "verify":
        ids = None if args.all else [UUID(job_id) for job_id in args.job_ids]
        return asyncio.run(verify(args.database_url, ids, out, err))
    handlers = {
        "submit": _submit,
        "status": _status,
        "logs": _logs,
        "answer": _answer,
        "approve": _approve,
        "deny": _deny,
        "cancel": _cancel,
    }
    try:
        with httpx2.Client(base_url=args.api_url, timeout=30.0, transport=transport) as client:
            if args.command == "list":
                return _list(client, args, out, err)
            return handlers[args.command](client, args, out)
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
