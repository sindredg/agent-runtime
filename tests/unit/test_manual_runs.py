"""The job of the Manual runs workflow that holds the API key (issue 130).

The model name reaches `make kills-claude` only after a check, and through
the environment, so a value can never become shell code. That job neither
restores nor saves the uv cache.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "manual-runs.yml"


def claude_job() -> str:
    """The text of the workflow's `claude` job."""
    text = WORKFLOW.read_text()
    return text[text.index("\n  claude:\n") :]


def kills_claude(
    model: str, tmp_path: Path, *, from_environment: bool = False
) -> tuple[int, bool, bool]:
    """Run `make kills-claude` with a stub docker.

    Returns the exit code, whether docker ran, and whether injected code ran.
    """
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    called = tmp_path / "docker-called"
    (stub / "docker").write_text(f"#!/bin/sh\ntouch {called}\nexit 1\n")
    (stub / "docker").chmod(0o755)
    done = subprocess.run(  # noqa: S603 - fixed program, the model is the input under test
        [  # noqa: S607
            "make",
            "--no-print-directory",
            "kills-claude",
            *([] if from_environment else [f"KILLS_MODEL={model}"]),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{stub}:{os.environ['PATH']}",
            "DOCKER_GID": "0",
            **({"KILLS_MODEL": model} if from_environment else {}),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return done.returncode, called.exists(), (tmp_path / "injected").exists()


@pytest.mark.parametrize(
    "model",
    [
        "",
        "claude-haiku-5-5; touch {tmp}/injected",
        'claude-x"; touch {tmp}/injected; "',
        "gpt-4",
        "claude-Haiku",
        "claude-",
        "claude-haiku-5-5\nanything else",
    ],
)
def test_a_model_name_that_is_not_an_id_stops_make_before_docker(
    model: str, tmp_path: Path
) -> None:
    code, docker_ran, injected = kills_claude(model.format(tmp=tmp_path), tmp_path)
    assert code != 0
    assert not docker_ran
    assert not injected


@pytest.mark.parametrize("from_environment", [False, True], ids=["command-line", "environment"])
@pytest.mark.parametrize(
    "model",
    ["claude-x$(shell touch {tmp}/injected)", "claude-x\n$(shell touch {tmp}/injected)"],
    ids=["make-function", "make-function-on-a-second-line"],
)
def test_make_code_in_the_model_name_runs_nothing(
    model: str, from_environment: bool, tmp_path: Path
) -> None:
    """make takes the value as raw text, so its own functions in it never run."""
    code, docker_ran, injected = kills_claude(
        model.format(tmp=tmp_path), tmp_path, from_environment=from_environment
    )
    assert not injected
    assert code != 0
    assert not docker_ran


def test_a_model_id_reaches_the_kill_runner_through_the_environment(tmp_path: Path) -> None:
    stub = tmp_path / "bin"
    stub.mkdir()
    seen = tmp_path / "uv-env"
    (stub / "docker").write_text("#!/bin/sh\nexit 0\n")
    # Only the two variables: the environment can hold a key.
    (stub / "uv").write_text(
        "#!/bin/sh\n"
        "printf 'KILLS_MODEL=%s\\nKILLS_PROVIDER=%s\\n'"
        f' "$KILLS_MODEL" "$KILLS_PROVIDER" > {seen}\n'
    )
    for program in ("docker", "uv"):
        (stub / program).chmod(0o755)
    done = subprocess.run(
        ["make", "--no-print-directory", "kills-claude", "KILLS_MODEL=claude-haiku-5-5"],  # noqa: S607
        cwd=ROOT,
        env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "DOCKER_GID": "0"},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    env = seen.read_text().splitlines()
    assert "KILLS_MODEL=claude-haiku-5-5" in env
    assert "KILLS_PROVIDER=claude" in env


def check_step() -> str:
    """The shell script of the claude job's model check."""
    job = claude_job()
    step = job[job.index("- name: Check the model name") :]
    script = step[step.index("run: |\n") + len("run: |\n") :]
    script = script[: script.index("\n      - ")]
    return "\n".join(line.removeprefix("          ") for line in script.splitlines())


def test_the_job_with_the_key_checks_the_model_before_anything_else() -> None:
    job = claude_job()
    steps = job[job.index("    steps:\n") :]
    assert steps.index("- name: Check the model name") < steps.index("actions/checkout@")


@pytest.mark.parametrize(
    ("model", "passes"),
    [
        ("claude-haiku-5-5", True),
        ("claude-fable-5-2", True),
        ("", False),
        ("claude-", False),
        ("gpt-4", False),
        ("claude-haiku-5-5\nclaude-x$(curl evil)", False),
        ("claude-x; echo", False),
    ],
)
def test_the_workflows_check_takes_only_a_whole_model_id(model: str, passes: bool) -> None:
    done = subprocess.run(  # noqa: S603 - the workflow's own check, run as GitHub runs it
        ["bash", "-e", "-c", check_step()],  # noqa: S607
        env={**os.environ, "MODEL": model},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert (done.returncode == 0) is passes, done.stderr


def test_the_job_with_the_key_uses_no_uv_cache() -> None:
    job = claude_job()
    setup = job[job.index("astral-sh/setup-uv@") :]
    setup = setup[: setup.index("\n      - ")]
    assert "enable-cache: false" in setup
