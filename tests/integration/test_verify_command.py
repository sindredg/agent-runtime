"""`runtime verify`: G11's "found by the verify command", run through the command itself.

The tampering is the owner's, with the triggers off, as in
`test_step_log_chain.py`. The command runs as the app role.
"""

import io

import pytest
from sqlalchemy import make_url

from runtime.cli import main

from .conftest import as_app
from .test_step_log_chain import TAMPERED, make_job, tamper

UNKNOWN = "00000000-0000-7000-8000-000000000000"
# A password the command must not print.
WRONG = "not-the-password-x9"


def run(database_url: str, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    status = main(["verify", "--database-url", database_url, *args], out=out, err=err)
    return status, out.getvalue(), err.getvalue()


def test_a_log_as_written_holds(database_url: str) -> None:
    job_id = make_job(database_url)

    assert run(database_url, str(job_id)) == (
        0,
        f"{job_id}: holds, 3 steps\n1 jobs checked, 0 with a mismatch\n",
        "",
    )


@pytest.mark.parametrize("case", TAMPERED)
def test_the_command_names_what_the_owner_changed(database_url: str, case: str) -> None:
    statements, expected = TAMPERED[case]
    job_id = make_job(database_url)
    tamper(database_url, job_id, *statements)

    status, out, err = run(database_url, str(job_id))

    named = [
        f"{job_id}: {'head' if m.seq is None else f'step {m.seq}'}: {m.reason}" for m in expected
    ]
    assert (status, err) == (1, "")
    assert out.splitlines() == [*named, "1 jobs checked, 1 with a mismatch"]


def test_all_names_only_the_jobs_with_a_mismatch(database_url: str) -> None:
    clean, changed = make_job(database_url), make_job(database_url)
    tamper(database_url, changed, "DELETE FROM steps WHERE job_id = $job AND seq = 3")

    status, out, _ = run(database_url, "--all")

    assert status == 1
    assert str(clean) not in out
    assert out.splitlines() == [
        f"{changed}: head: head is step 3, the log ends at 2",
        "2 jobs checked, 1 with a mismatch",
    ]


def test_a_job_that_cannot_be_checked_is_not_a_pass(database_url: str) -> None:
    job_id = make_job(database_url)

    assert run(database_url, UNKNOWN) == (
        2,
        "0 jobs checked, 0 with a mismatch\n",
        f"{UNKNOWN}: no such job\n",
    )
    # A mismatch outranks a job that could not be checked.
    tamper(database_url, job_id, "UPDATE steps SET kind = 'error' WHERE job_id = $job AND seq = 1")
    assert run(database_url, UNKNOWN, str(job_id))[0] == 1


def test_a_database_it_cannot_reach_is_not_a_pass_and_the_password_is_not_printed(
    database_url: str,
) -> None:
    wrong = make_url(as_app(database_url)).set(password=WRONG)
    url = wrong.render_as_string(hide_password=False)

    status, out, err = run(url, "--all")

    assert (status, out) == (2, "")
    assert "password authentication failed" in err
    assert WRONG not in err


def test_a_url_it_cannot_read_is_not_printed() -> None:
    status, out, err = run("no url, but a secret-x9", "--all")

    assert (status, out) == (2, "")
    assert err == "runtime: cannot check: the database URL cannot be read\n"


def test_an_empty_database_holds(database_url: str) -> None:
    assert run(database_url, "--all") == (0, "0 jobs checked, 0 with a mismatch\n", "")
