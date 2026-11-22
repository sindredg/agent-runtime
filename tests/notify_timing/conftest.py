"""The timed run of G12 needs the stack of `make up` on the fake provider."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_sessionstart(session: pytest.Session) -> None:
    """End the session before any test unless every worker is on the fake provider."""
    check = subprocess.run(  # noqa: S603 - fixed program and script
        [sys.executable, str(REPO_ROOT / "scripts" / "check_fake_provider.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        pytest.exit(check.stderr.strip(), returncode=pytest.ExitCode.USAGE_ERROR)
