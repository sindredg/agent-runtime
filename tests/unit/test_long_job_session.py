"""The long-job harness, without a stack and without a key.

`tests/long_job/conftest.py` is imported as pytest imports it. Docker is a
stub that records each command, and the API a mock transport that records
each request: the session's refusals, the worker check, what is submitted
and written, the kills, the pass line and the record are checked here. The
keyed sessions run against the same stubs, with a seed of the tests' own and
a recorder in place of the SDK's client.
"""

import importlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import httpx2
import pytest

from access_review import SHORT, answer_key, generate, read_seed

ROOT = Path(__file__).resolve().parents[2]
SEED_FILE = ROOT / "tests" / "unit" / "access_review_seed.json"
# A seed of the tests' own. Not the committed one.
OTHER_SEED = "00112233445566778899aabbccddeeff"
FAKE_URL = "http://fake-provider:8000"
CLAUDE_URL = "https://api.anthropic.com"
# What the long stack reads from the shell. The harness sets each on every compose call.
SETTINGS = (
    "LONG_SCENARIO_DIR",
    "SUMMARY_TRIGGER",
    "FAILPOINT",
    "FAILPOINT_SUMMARY",
    "FAKE_TRACKER_FIRST_ISSUE",
)
WORKER_IDS = ("a" * 64, "b" * 64)
TRACKER_ID = "c" * 64
# What the stub API says when it refuses a job. A record never holds it.
API_WORDS = "the stub API refuses this job"
RUN_FILE = ROOT / "tests" / "long_job" / "test_long_job_run.py"
# The five tests of the job's own, each with the fixed message of its assert.
JOB_TESTS = {
    "test_every_counted_violation_of_a_notice_has_an_issue": (
        "a counted notice-violating grant has no issue"
    ),
    "test_no_counted_grant_a_notice_clears_has_an_issue": (
        "a counted notice-cleared grant has an issue"
    ),
    "test_every_issue_after_the_first_summary_holds_the_reference_code": (
        "an issue after the first summary lacks the reference code"
    ),
    "test_the_log_holds": "the log, the tools or the issues break a rule, or the runtime failed",
    "test_the_run_shows_enough": "the run is under a floor of the pass line",
}


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The harness's conftest, imported as pytest imports it. It starts no stack."""
    monkeypatch.syspath_prepend(str(ROOT / "tests"))
    return importlib.import_module("long_job.conftest")


@pytest.fixture(scope="module")
def review() -> Any:
    return generate(read_seed(SEED_FILE), SHORT)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No LONG_JOB_* variable from the shell running the tests."""
    for name in list(os.environ):
        if name.startswith("LONG_JOB_"):
            monkeypatch.delenv(name)
    return monkeypatch


class Docker:
    """A stand-in for `docker`: two workers on `url`, a tracker, and every command recorded."""

    def __init__(self, url: str = FAKE_URL, lease: str = "30") -> None:
        self.url, self.lease = url, lease
        self.calls: list[tuple[str, ...]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
        self.calls.append(args)
        if env is not None:
            self.envs.append(env)
        if args[:2] == ("docker", "ps"):
            if "label=com.docker.compose.service=worker" in args:
                return "".join(f"{worker} running\n" for worker in WORKER_IDS)
            return f"{TRACKER_ID} running\n"
        if "printenv" in args:
            return {"ANTHROPIC_BASE_URL": self.url, "LEASE_SECONDS": self.lease}[args[-1]] + "\n"
        if args[:2] == ("docker", "inspect"):
            return f"{TRACKER_ID} 2026-11-26T00:00:00Z\n"
        return ""


class Api:
    """The API as a mock transport: an empty job list, and `POST /jobs` answered with `status`."""

    def __init__(self, status: int = 500) -> None:
        self.status = status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/jobs":
            return httpx2.Response(self.status, json={"detail": API_WORDS})
        return httpx2.Response(200, json={"jobs": []})

    def posted(self) -> list[bytes]:
        return [r.content for r in self.requests if r.method == "POST" and r.url.path == "/jobs"]


def stack(harness: ModuleType, directory: Path, docker: Docker, api: Api, **more: Any) -> Any:
    client = httpx2.Client(transport=httpx2.MockTransport(api), base_url="http://api")
    tracker = httpx2.Client(transport=httpx2.MockTransport(api), base_url="http://tracker")
    return harness.Stack(directory, command=docker, api=client, tracker=tracker, **more)


def must_fail_env(monkeypatch: pytest.MonkeyPatch, **more: str) -> None:
    for name, value in {
        "LONG_JOB_PROVIDER": "fake",
        "LONG_JOB_MODE": "must-fail",
        "LONG_JOB_PHASE": "control",
        **more,
    }.items():
        monkeypatch.setenv(name, value)


# What a session may start with


def test_every_keyed_session_is_refused_while_the_line_is_proposed(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each mode on the Claude API, with every input the run asks for given, and no date."""
    monkeypatch.setattr(harness.rules, "LINE_FIXED", None)
    budgets = {"counts": "1500000", "graded": "20000000", "must-fail": ""}
    for mode, budget in budgets.items():
        env = {
            "LONG_JOB_PROVIDER": "claude",
            "LONG_JOB_MODE": mode,
            "LONG_JOB_PHASE": "control",
            "LONG_JOB_SEED": OTHER_SEED,
            "LONG_JOB_TOKEN_BUDGET": budget,
        }
        found = harness.session_problems(env, ROOT)
        assert "The pass line is proposed." in found, mode
        assert OTHER_SEED not in " ".join(found)


def test_a_must_fail_session_on_the_fake_provider_may_start(harness: ModuleType) -> None:
    env = {"LONG_JOB_PROVIDER": "fake", "LONG_JOB_MODE": "must-fail", "LONG_JOB_PHASE": "control"}
    assert harness.session_problems(env, ROOT) == []
    assert harness.session_problems({**env, "LONG_JOB_PROVIDER": ""}, ROOT) == []
    assert harness.session_problems({**env, "LONG_JOB_PHASE": "edited"}, ROOT) == []
    # The seed is not read on the fake: a value that is no seed changes nothing.
    assert harness.session_problems({**env, "LONG_JOB_SEED": "not a seed"}, ROOT) == []


@pytest.mark.parametrize(
    ("changed", "sentence"),
    [
        ({"LONG_JOB_PROVIDER": "Claude"}, "The provider is not fake or claude."),
        ({"LONG_JOB_TOKEN_BUDGET": "1500000"}, "A must-fail run takes no token budget."),
        ({"LONG_JOB_MODE": ""}, "The mode is not counts, must-fail or graded."),
        ({"LONG_JOB_MODE": "must_fail"}, "The mode is not counts, must-fail or graded."),
        ({"LONG_JOB_PHASE": ""}, "The phase is not control or edited."),
        ({"LONG_JOB_PHASE": "Control"}, "The phase is not control or edited."),
    ],
)
def test_each_refusal_of_a_session_on_the_fake_provider_alone(
    harness: ModuleType, changed: dict[str, str], sentence: str
) -> None:
    env = {"LONG_JOB_PROVIDER": "fake", "LONG_JOB_MODE": "must-fail", "LONG_JOB_PHASE": "control"}
    assert harness.session_problems({**env, **changed}, ROOT) == [sentence]


def start_session(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch, docker: Any = None
) -> tuple[str, Any]:
    """Start a session as pytest does, with docker recorded and no API reached.

    Returns how it stopped: the exit's message, "the session started", or the
    name of what escaped, so every outcome fails an assertion of the caller.
    """
    docker = Docker() if docker is None else docker
    monkeypatch.setattr(harness, "docker", docker)
    # The job list of a stack that may be running on this machine is never read.
    monkeypatch.setattr(harness.Stack, "unfinished", lambda _: 0)

    class Config:
        stash = pytest.Stash()

    class Session:
        config = Config()

    try:
        harness.pytest_sessionstart(Session())
        said = "the session started"
    except pytest.exit.Exception as stopped:
        said = str(stopped.msg)
    except Exception as error:
        said = f"{type(error).__name__} escaped"
    return said, docker


def test_a_keyed_session_stops_on_its_refusals_before_any_stack_call(
    harness: ModuleType, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setattr(harness.rules, "LINE_FIXED", None)
    must_fail_env(clean_env, LONG_JOB_PROVIDER="claude", LONG_JOB_SEED=OTHER_SEED)
    said, docker = start_session(harness, clean_env)

    assert ("The pass line is proposed." in said, docker.calls) == (True, [])
    assert said.startswith("Not run: ")
    assert OTHER_SEED not in said


def test_a_misspelt_provider_stops_the_session_before_any_stack_call(
    harness: ModuleType, clean_env: pytest.MonkeyPatch
) -> None:
    must_fail_env(clean_env, LONG_JOB_PROVIDER="Claude")
    said, docker = start_session(harness, clean_env)

    assert (said, docker.calls) == ("Not run: The provider is not fake or claude.", [])


def test_a_must_fail_session_given_a_budget_stops_before_any_stack_call(
    harness: ModuleType, clean_env: pytest.MonkeyPatch
) -> None:
    must_fail_env(clean_env, LONG_JOB_TOKEN_BUDGET="1500000")  # noqa: S106 - a count of tokens
    said, docker = start_session(harness, clean_env)

    assert (said, docker.calls) == ("Not run: A must-fail run takes no token budget.", [])


def test_a_session_that_may_start_checks_the_workers_first(
    harness: ModuleType, clean_env: pytest.MonkeyPatch
) -> None:
    """The stub has workers on the Claude API, so the worker check stops it."""
    must_fail_env(clean_env)
    said, docker = start_session(harness, clean_env, Docker(url=CLAUDE_URL))

    assert "this run needs http://fake-provider:8000" in said
    assert [call[:2] for call in docker.calls] == [("docker", "ps"), ("docker", "exec")]


def test_a_session_stops_with_a_sentence_when_docker_does_not_answer(
    harness: ModuleType, clean_env: pytest.MonkeyPatch
) -> None:
    must_fail_env(clean_env)

    def broken(*args: str, **_: Any) -> str:
        raise RuntimeError(f"{' '.join(args[:3])} ... exited 1")

    said, _ = start_session(harness, clean_env, broken)

    assert said == "Not run: Docker did not answer (RuntimeError)."


# Where the session's clock starts


def test_the_clock_starts_when_the_stack_is_up_and_checked(harness: ModuleType) -> None:
    rules = harness.rules
    ready = 1000.0
    # A full job waits the whole session, so it starts only at 0 s.
    assert harness.may_submit(ready, ready, rules.FULL) is True
    assert harness.may_submit(ready, ready + 0.001, rules.FULL) is False
    # A short job waits 40 minutes of the session's 320.
    last = ready + (rules.HARNESS_MINUTES - rules.SHORT.job_minutes) * 60
    assert harness.may_submit(ready, last, rules.SHORT) is True
    assert harness.may_submit(ready, last + 1, rules.SHORT) is False
    # Before the stack is checked no job starts.
    assert harness.may_submit(None, ready, rules.SHORT) is False


# What the session submits and writes


def run_session(
    harness: ModuleType, directory: Path, docker: Docker, api: Api
) -> tuple[dict[str, Any], Docker, Api]:
    found = stack(harness, directory, docker, api)
    record = found.run()
    found.hang_up()
    return record, docker, api


def test_a_stack_on_another_provider_is_sent_no_job(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for provider, wrong in (("fake", CLAUDE_URL), ("claude", FAKE_URL)):
        env = {"LONG_JOB_SEED": OTHER_SEED}
        found = stack(
            harness,
            tmp_path / provider,
            Docker(url=wrong),
            api := Api(),
            provider=provider,
            env=env,
        )
        record = found.run()

        assert (api.posted(), record["harness_error"]) == ([], "WrongStack")
        assert (record["verdict"], record["voids"]) == ("VOID", ["harness_error"])
        assert record["clauses"]["void_harness_error"] == 1


def test_a_stack_on_the_runs_provider_is_sent_the_job(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API refuses it here: the session records the status code only, and is void."""
    record, _, api = run_session(harness, tmp_path, Docker(), Api(status=500))
    body = json.loads(api.posted()[0])

    assert len(api.posted()) == 1
    assert (record["harness_error"], record["submission_status"]) == ("SubmissionRefused", 500)
    assert record["verdict"] == "VOID"
    assert body["model"] == "fake-long-job"
    assert (body["token_budget"], body["timeout_seconds"]) == (4_000_000, 3_600)
    assert len(body["files"]) == 37
    assert API_WORDS not in json.dumps(record)


def test_the_stack_is_started_with_every_setting_of_the_run(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setenv("SUMMARY_TRIGGER", "3000")
    clean_env.setenv("FAILPOINT", "after_model_request")
    _, docker, _ = run_session(harness, tmp_path, Docker(), Api())
    # The stack's settings only: the rest of the environment may hold a key.
    (settings,) = [{key: env.get(key) for key in SETTINGS} for env in docker.envs]
    scenarios = tmp_path / "scenarios"

    assert docker.calls[0][:7] == (*harness.COMPOSE, "up")
    assert settings == {
        "LONG_SCENARIO_DIR": str(scenarios),
        "SUMMARY_TRIGGER": "20000",
        "FAILPOINT": "after_summary",
        "FAILPOINT_SUMMARY": "",
        "FAKE_TRACKER_FIRST_ISSUE": "",
    }
    assert [path.name for path in scenarios.iterdir()] == ["fake-long-job.json"]
    scenario = json.loads((scenarios / "fake-long-job.json").read_text())
    assert scenario["summary"].get("delay_seconds") == 2.0
    assert {turn.get("delay_seconds") for turn in scenario["turns"]} == {2.0}


def test_the_fake_run_never_reads_the_seed_of_the_environment(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A set LONG_JOB_SEED changes nothing the session submits, writes or gives compose."""
    seen = []
    for name, seed in (("unset", None), ("set", OTHER_SEED)):
        if seed is not None:
            clean_env.setenv("LONG_JOB_SEED", seed)
        record, docker, api = run_session(harness, tmp_path / name, Docker(), Api())
        scenario = (tmp_path / name / "scenarios" / "fake-long-job.json").read_bytes()
        # Only the stack's settings are compared: an assert that fails prints
        # what it compares, and the rest of the environment may hold a key.
        # The scenario directory is the session's own.
        settings = [{key: env.get(key) for key in SETTINGS[1:]} for env in docker.envs]
        given = [key for env in docker.envs for key in env if key.startswith("LONG_JOB_")]
        seen.append((api.posted(), scenario, settings, given, record["seed"]))

    assert seen[0] == seen[1]
    assert seen[0][3:] == ([], "committed")
    assert len(seen[0][0]) == 1


def test_an_error_of_the_generator_is_kept_as_its_type_only(
    harness: ModuleType,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    review: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = review.reference_code

    def broken(seed: bytes, shape: Any) -> Any:
        raise ValueError(f"cannot make the job of {code}")

    clean_env.setattr(harness, "generate", broken)
    record, docker, api = run_session(harness, tmp_path, Docker(), Api())
    written = (tmp_path / "results.jsonl").read_text()
    printed = capsys.readouterr().out

    assert (record["harness_error"], record["verdict"]) == ("ValueError", "VOID")
    assert (docker.calls, api.posted()) == ([], [])
    assert code not in written + printed
    assert printed.strip() == f"RESULT {written.strip()}"


def test_the_record_names_its_run(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record, _, _ = run_session(harness, tmp_path, Docker(), Api())

    assert {key: record[key] for key in ("test", "runs", "mode", "phase", "shape")} == {
        "test": "long-job",
        "runs": 1,
        "mode": "must-fail",
        "phase": "control",
        "shape": "short",
    }
    assert (record["provider"], record["model"]) == ("fake provider", "fake-long-job")
    keyed = harness.record_line(
        stack(harness, tmp_path / "keyed", Docker(), Api(), provider="claude"),
        harness.rules.SHORT,
        harness.Seen(),
        None,
    )
    assert (keyed["provider"], keyed["model"]) == ("Claude API", "claude-haiku-5-5")
    assert (record["workers"], record["lease_seconds"], record["token_budget"]) == (
        2,
        30.0,
        4_000_000,
    )
    assert record["seed"] == "committed"
    assert record["where"] in ("local Docker Compose", "GitHub Actions")
    assert record["grading_error"] is None


def test_the_record_and_its_printed_line_hold_no_workflow_command(
    harness: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = stack(harness, tmp_path, Docker(), Api())
    found.write({"verdict": "VOID", "note": "::error::x ##[error]y"})
    found.hang_up()
    written = (tmp_path / "results.jsonl").read_text()
    printed = capsys.readouterr().out

    assert "##[" not in written + printed
    assert json.loads(written)["note"] == "::error::x ##[error]y"
    assert "\n::" not in "\n" + printed.replace("\nRESULT ", "\n")


# Kills


def summary_steps(*seqs: int) -> list[dict[str, Any]]:
    return [{"seq": seq, "kind": "summary"} for seq in seqs]


def test_the_kills_of_the_committed_seed(harness: ModuleType) -> None:
    rules = harness.rules
    planned = harness.plan(read_seed(SEED_FILE), rules.SHORT)
    offsets = [rules.offset(read_seed(SEED_FILE), "short", i) for i in (2, 3)]

    assert [(kill.number, kill.after) for kill in planned] == [(2, 2), (3, 4)]
    assert [kill.offset for kill in planned] == offsets
    assert [kill.target for kill in planned] == [None, None]


def test_a_kill_aims_at_its_offset_after_its_summary(harness: ModuleType) -> None:
    steps = [{"seq": n, "kind": "model_request"} for n in range(1, 40)]
    steps[9] = {"seq": 10, "kind": "summary"}
    planned = harness.Planned(2, after=2, offset=8)

    assert harness.kill_target(steps, 2, 8) is None
    steps[19] = {"seq": 20, "kind": "summary"}
    planned.target = harness.kill_target(steps, 2, 8)
    assert planned.target == 28
    assert harness.due(planned, steps[:27]) is False
    assert harness.due(planned, steps[:28]) is True
    assert harness.due(planned, steps[:30]) is True
    planned.kill = harness.grader.Kill(28, 1, held=True)
    assert harness.due(planned, steps[:30]) is False


def test_the_kill_aims_at_the_worker_that_holds_the_job(
    harness: ModuleType, tmp_path: Path
) -> None:
    docker = Docker()
    owner = f"{WORKER_IDS[1][:12]}-7"

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, json={"status": "running", "lease_owner": owner, "lease_epoch": 4, "spec": {}}
        )

    client = httpx2.Client(transport=httpx2.MockTransport(answer), base_url="http://api")
    found = harness.Stack(tmp_path, command=docker, api=client, tracker=client)
    found.containers = list(WORKER_IDS)
    created = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    step = {"seq": 28, "kind": "model_request", "lease_epoch": 3, "created_at": created}
    kill = found.kill("job", harness.Planned(2, 2, 8, target=28), [step])
    found.hang_up()

    assert docker.calls[-2:] == [
        ("docker", "kill", WORKER_IDS[1]),
        ("docker", "start", WORKER_IDS[1]),
    ]
    # The epoch is the view's, read with its owner, not the last step's: the
    # job was taken over after that step.
    assert (kill.target, kill.epoch, kill.held, kill.problem) == (28, 4, True, None)
    assert kill.latency is not None and kill.latency >= 1.0


# The workers' logs


def log_line(when: str, message: str, **more: Any) -> str:
    return f"{when} " + json.dumps({"message": message, **more})


def test_the_failpoint_lines_of_the_run_are_counted_by_their_message(harness: ModuleType) -> None:
    fired = "failpoint reached, stopping the worker"
    text = "\n".join(
        [
            log_line("2026-11-26T10:00:00.5Z", fired),
            log_line("2026-11-26T10:00:02.000001Z", fired, failpoint="after_summary"),
            log_line("2026-11-26T10:00:03Z", "job finished", error=fired),
            "2026-11-26T10:00:04Z " + fired,
            log_line("2026-11-26T10:00:05Z", fired),
        ]
    )
    run = harness.since(text, datetime(2026, 11, 26, 10, 0, 1, tzinfo=UTC))

    assert harness.failpoint_lines(text) == 3
    assert harness.failpoint_lines(run) == 2
    assert len(run.splitlines()) == 4


# The pass line of the fake run


def passing() -> dict[str, Any]:
    kills = [{"landed": True, "window": 2}, {"landed": True, "window": 4}]
    return {
        "verdict": "PASS",
        "failpoint_lines": 1,
        "takeovers": 3,
        "questions": 0,
        "floors": {"summaries": 6},
        "counts": {"kills": kills},
    }


def broken(change: Mapping[str, Any], **kills: Any) -> dict[str, Any]:
    record = {**passing(), **change}
    if kills:
        record["counts"] = {"kills": [dict(kill) for kill in passing()["counts"]["kills"]]}
        for index, value in kills.items():
            record["counts"]["kills"][int(index[1:])].update(value)
    return record


@pytest.mark.parametrize(
    ("record", "missed"),
    [
        (broken({"verdict": "INCONCLUSIVE"}), ["verdict"]),
        (broken({"failpoint_lines": 0}), ["failpoint"]),
        (broken({"failpoint_lines": 2}), ["failpoint"]),
        (broken({}, k0={"landed": False}), ["kill_2", "kill_windows"]),
        (broken({}, k1={"landed": False}), ["kill_3", "kill_windows"]),
        (broken({}, k1={"window": 2}), ["kill_windows"]),
        (broken({"takeovers": 2}), ["takeovers"]),
        (broken({"takeovers": None}), ["takeovers"]),
        (broken({"questions": 1}), ["questions"]),
        (broken({"floors": {"summaries": 5}}), ["summaries"]),
        (broken({"floors": {}}), ["summaries"]),
        (broken({"counts": {}}), ["kill_2", "kill_3", "kill_windows"]),
    ],
)
def test_each_item_of_the_fake_runs_pass_line_alone(
    harness: ModuleType, record: dict[str, Any], missed: list[str]
) -> None:
    assert harness.pass_line_misses(passing()) == []
    assert harness.pass_line_misses(record) == missed


# What may not be printed, recorded or kept


def test_the_answers_looked_for(harness: ModuleType, review: Any) -> None:
    key = answer_key(review)
    found = harness.markers(review)
    carried = [p.grant.id for p in review.planted if p.kind == "carried"]

    assert {review.reference_code, *key.made, *key.cleared, *carried} <= set(found)
    assert {notice.text for notice in review.notices} <= set(found)
    assert "access records, file 0001 of 0036" in found
    assert "access/0001.txt" in found
    assert harness.markers_in(["nothing", f"x {key.made[0]} y {review.reference_code}"], found) == 2


def makefile_flags(target: str) -> list[str]:
    """The options the Makefile gives pytest in a target's recipe."""
    lines = (ROOT / "Makefile").read_text().split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
    words = lines.replace("\\\n", " ").split("pytest", 1)[1].split()
    return [word for word in words if word.startswith("-")]


def test_make_long_job_runs_the_fake_provider_must_fail_control_with_short_tracebacks() -> None:
    recipe = (ROOT / "Makefile").read_text().split("\nlong-job:", 1)[1].split("\n\n", 1)[0]

    assert recipe.split("\n", 1)[0].split() == ["long-up"]
    for setting in ("LONG_JOB_PROVIDER=fake", "LONG_JOB_MODE=must-fail", "LONG_JOB_PHASE=control"):
        assert setting in recipe.split()
    assert makefile_flags("long-job") == ["-v", "--tb=short"]
    assert "LONG_JOB_TOKEN_BUDGET" not in recipe
    assert "LONG_JOB_SEED" not in recipe


def run_job_tests(directory: Path, record: Mapping[str, Any]) -> tuple[int, str]:
    """Run the five tests of the job's own as `make long-job` runs them, on `record`.

    The record comes from a plugin of the test's own, in place of the harness.
    """
    (directory / "sample_record.py").write_text(
        "import json\n\nimport pytest\n\n\n@pytest.fixture\n"
        "def record() -> dict[str, object]:\n"
        f"    return json.loads({json.dumps(json.dumps(record))})\n"
    )
    env = {
        **{name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")},
        "PYTHONPATH": os.pathsep.join([str(directory), os.environ.get("PYTHONPATH", "")]),
    }
    done = subprocess.run(  # noqa: S603 - the test's own command
        [
            sys.executable,
            "-m",
            "pytest",
            *makefile_flags("long-job"),
            "--noconftest",
            "-p",
            "sample_record",
            "-p",
            "no:cacheprovider",
            *(f"{RUN_FILE.relative_to(ROOT)}::{name}" for name in JOB_TESTS),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )
    return done.returncode, done.stdout + done.stderr


def test_the_jobs_own_tests_print_their_messages_and_nothing_of_the_job(
    review: Any, tmp_path: Path
) -> None:
    """On a record that fails each of the five tests.

    The record also holds an id, the code and a title. None of them is in
    the output: each test binds a count, and the traceback is short.
    """
    key = answer_key(review)
    title = f"{key.made[0]}: access to remove, {review.reference_code}"
    record = {
        "clauses": {"R7": 3, "R8": 2, "R9": 1, "R5": 1, "floor_made": 1},
        "floors": {"made": 5},
        "verdict": "FAIL_RUNTIME",
        "provider": "fake provider",
        "note": [key.cleared[0], review.reference_code, title],
    }
    code, output = run_job_tests(tmp_path, record)

    assert code == 1, output[-2000:]
    assert "5 failed" in output
    for message in JOB_TESTS.values():
        assert f"AssertionError: {message}" in output
    leaked = [text for text in (key.cleared[0], review.reference_code, title) if text in output]
    assert leaked == []
    assert "assert 3 == 0" in output


def test_a_record_with_no_floor_measured_fails_only_the_test_of_the_floors(
    tmp_path: Path,
) -> None:
    """A void run before the job could be graded: no clause but its void, and no floors."""
    record = {"clauses": {"void_harness_error": 1}, "floors": {}, "verdict": "VOID"}
    code, output = run_job_tests(tmp_path, record)

    assert code == 1, output[-2000:]
    assert "1 failed, 4 passed" in output
    assert f"AssertionError: {JOB_TESTS['test_the_run_shows_enough']}" in output


@pytest.mark.parametrize("clause", ["R2", "R5", "R6", "crash", "runtime"])
def test_a_runtime_failure_of_the_job_fails_only_the_test_of_the_log(
    tmp_path: Path, clause: str
) -> None:
    """A job the runtime failed (its class "runtime") breaks the log as a crash does."""
    record = {"clauses": {clause: 1}, "floors": {"made": 7}, "verdict": "FAIL_RUNTIME"}
    code, output = run_job_tests(tmp_path, record)

    assert code == 1, output[-2000:]
    assert "1 failed, 4 passed" in output
    assert f"AssertionError: {JOB_TESTS['test_the_log_holds']}" in output


def test_the_tests_of_the_run_collect_without_the_harness() -> None:
    """As the switched-off list collects its tests: with `--noconftest`, off the stack."""
    done = subprocess.run(  # noqa: S603 - a fixed command
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--noconftest",
            "-p",
            "no:cacheprovider",
            str(RUN_FILE.relative_to(ROOT)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")},
        timeout=120,
    )
    collected = [line for line in done.stdout.splitlines() if "::" in line]

    assert done.returncode == 0, done.stdout[-2000:]
    assert [line.split("::")[1] for line in collected] == [
        *JOB_TESTS,
        "test_fake_run_pass_line",
        "test_a_graded_run_is_green_only_on_pass",
        "test_every_job_of_a_counts_run_ends_and_is_recorded",
        "test_no_answer_is_in_the_record_the_output_or_the_saved_logs",
    ]


def test_every_assert_of_the_jobs_own_tests_is_one_line_with_its_message() -> None:
    """A message on its own line would not be the line pytest shows under a short traceback."""
    text = RUN_FILE.read_text()
    for name, message in JOB_TESTS.items():
        body = text.split(f"def {name}(", 1)[1].split("\ndef ", 1)[0]
        (line,) = [line for line in body.splitlines() if line.strip().startswith("assert ")]
        assert re.fullmatch(rf'    assert \w+ == 0, "{re.escape(message)}"', line), line


# A keyed session: Docker, the API and the count stubbed, a seed of the tests' own

NOT_A_KEY = "sk-ant-not-a-key-of-the-tests"


def keyed_env(mode: str, **more: str) -> dict[str, str]:
    """The environment of a keyed run as `make long-job-claude` gives it, with a test seed."""
    return {
        "PATH": os.environ["PATH"],
        "LONG_JOB_PROVIDER": "claude",
        "LONG_JOB_MODE": mode,
        "LONG_JOB_PHASE": "control",
        "LONG_JOB_SEED": OTHER_SEED,
        "CLAUDE_API_KEY": NOT_A_KEY,
        **more,
    }


def job_steps(review: Any, job_id: str) -> list[dict[str, Any]]:
    """A finished job that read a corpus file and filed one issue, its answers in its steps."""
    key = answer_key(review)
    path, text = list(review.files.items())[1]
    title = f"{key.made[0]}: access to remove, {review.reference_code}"
    when = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    tools = ["read_file", "create_issue"]

    def step(seq: int, kind: str, **payload: Any) -> dict[str, Any]:
        created = (when + timedelta(seconds=seq)).isoformat()
        return {
            "seq": seq,
            "kind": kind,
            "payload": payload,
            "lease_epoch": 1,
            "created_at": created,
        }

    def use(number: int, name: str, given: dict[str, Any]) -> dict[str, Any]:
        return {"type": "tool_use", "id": f"toolu_{number}", "name": name, "input": given}

    usage = {"input_tokens": 1000, "output_tokens": 50}
    issue = {"title": title, "body": f"Found in {path}."}
    return [
        step(1, "model_request", tools=tools, max_tokens=32000, summary_trigger=20000),
        step(2, "model_response", content=[use(1, "read_file", {"path": path})], usage=usage),
        step(3, "tool_intent", tool_use_id="toolu_1", name="read_file", input={"path": path}),
        step(4, "tool_result", tool_use_id="toolu_1", content=text, is_error=False),
        step(5, "model_request", tools=tools, max_tokens=32000, summary_trigger=20000),
        step(6, "model_response", content=[use(2, "create_issue", issue)], usage=usage),
        step(7, "tool_intent", tool_use_id="toolu_2", name="create_issue", input=issue),
        step(8, "tool_result", tool_use_id="toolu_2", content="issue 100001", is_error=False),
        step(9, "model_request", tools=tools, max_tokens=32000, summary_trigger=20000),
        step(10, "model_response", content=[{"type": "text", "text": "Done."}], usage=usage),
    ]


class Finished:
    """The API and the tracker of a stack whose every job succeeds at once."""

    def __init__(self, review: Any) -> None:
        self.review = review
        self.requests: list[httpx2.Request] = []
        self.jobs: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/jobs":
            self.jobs.append(f"job-{len(self.jobs) + 1}")
            return httpx2.Response(201, json={"id": self.jobs[-1]})
        if path == "/jobs":
            return httpx2.Response(200, json={"jobs": [self.view(job) for job in self.jobs]})
        if path == "/issues":
            title = job_steps(self.review, "")[5]["payload"]["content"][0]["input"]["title"]
            return httpx2.Response(
                200, json=[{"operation_id": f"{j}:toolu_2", "title": title} for j in self.jobs]
            )
        job = path.split("/")[2]
        if path.endswith("/steps"):
            after = int(request.url.params.get("after", 0))
            steps = [s for s in job_steps(self.review, job) if s["seq"] > after]
            return httpx2.Response(200, json={"steps": steps})
        return httpx2.Response(200, json=self.view(job))

    def view(self, job: str) -> dict[str, Any]:
        return {"id": job, "status": "succeeded", "takeovers": 0, "spec": {"prompt": "x"}}

    def posted(self) -> list[dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.method == "POST" and r.url.path == "/jobs"
        ]


class Counted:
    """Stands in for `anthropic.Anthropic`: keeps how it was built, and counts bytes plus 7."""

    built: ClassVar[list[dict[str, Any]]] = []
    fails: ClassVar[Exception | None] = None

    def __init__(self, **given: Any) -> None:
        Counted.built.append(given)
        self.messages = self

    def count_tokens(self, *, model: str, messages: list[dict[str, Any]]) -> Any:
        if Counted.fails is not None:
            raise Counted.fails
        return SimpleNamespace(input_tokens=len(messages[0]["content"].encode()) + 7)


def keyed_session(
    harness: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    env: dict[str, str],
    shape: Any = SHORT,
) -> tuple[list[dict[str, Any]], Docker, Finished, Any]:
    """Run a keyed session on the stubs. Returns its lines, docker, the API and the review."""
    import anthropic

    Counted.built = []
    monkeypatch.setattr(anthropic, "Anthropic", Counted)
    monkeypatch.setattr(harness, "verify", lambda job_id: 0)
    review = generate(bytes.fromhex(OTHER_SEED), shape)
    docker, api = Docker(url=CLAUDE_URL), Finished(review)
    client = httpx2.Client(transport=httpx2.MockTransport(api), base_url="http://api")
    found = harness.Stack(
        directory,
        provider="claude",
        mode=env["LONG_JOB_MODE"],
        command=docker,
        api=client,
        tracker=client,
        env=env,
    )
    lines = found.run_counts() if found.mode == "counts" else [found.run()]
    found.hang_up()
    assert found.markers == harness.markers(review)
    return lines, docker, api, review


def written_text(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(directory.rglob("*")) if path.is_file()
    )


def test_a_keyed_session_runs_its_own_seed_writes_no_scenario_and_keeps_no_answer(
    harness: ModuleType,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines, docker, api, review = keyed_session(harness, clean_env, tmp_path, keyed_env("must-fail"))
    (record,) = lines
    printed = capsys.readouterr()
    (body,) = api.posted()
    (compose,) = [
        env for call, env in zip(docker.calls, docker.envs, strict=False) if "compose" in call
    ]
    answers = harness.markers(review)
    committed = harness.markers(generate(read_seed(SEED_FILE), SHORT))

    assert (body["model"], body["token_budget"], body["timeout_seconds"]) == (
        "claude-haiku-5-5",
        4_000_000,
        3_600,
    )
    # The job is the seed's own, not the committed seed's.
    assert body["files"] == review.files
    assert set(answers) - set(committed)
    assert docker.calls[0][:9] == (*harness.COMPOSE, "-f", "docker-compose.claude.yml", "up")
    assert {key: compose.get(key) for key in SETTINGS} == {
        "LONG_SCENARIO_DIR": "",
        "SUMMARY_TRIGGER": "32000",
        "FAILPOINT": "after_summary",
        "FAILPOINT_SUMMARY": "",
        "FAKE_TRACKER_FIRST_ISSUE": "",
    }
    assert [key for key in compose if key.startswith("LONG_JOB_")] == []
    assert not (tmp_path / "scenarios").exists()
    assert (record["seed"], record["provider"], record["model"]) == (
        "private",
        "Claude API",
        "claude-haiku-5-5",
    )
    # The job ran and was graded: its answers were in its steps and titles.
    assert (record["harness_error"], record["clauses"].get("R7", 0)) == (None, 0)
    texts = [written_text(tmp_path), json.dumps(record), printed.out + printed.err]
    assert harness.markers_in(texts, answers) == 0
    assert [OTHER_SEED in text for text in texts] == [False] * 3
    # A must-fail job is not counted: no client is built.
    assert Counted.built == []


def test_a_keyed_graded_session_counts_the_job_once_with_the_key_on_the_full_shape(
    harness: ModuleType,
    clean_env: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from access_review import FULL
    from runtime.provider.messages import CLAUDE_API_URL

    env = keyed_env("graded", LONG_JOB_TOKEN_BUDGET="2000000")  # noqa: S106 - a count of tokens
    (record,), docker, api, review = keyed_session(harness, clean_env, tmp_path, env, FULL)
    printed = capsys.readouterr()
    compose = docker.envs[0]

    assert (compose["SUMMARY_TRIGGER"], compose["FAILPOINT"]) == ("32000", "after_summary")
    assert api.posted()[0]["token_budget"] == 2_000_000
    assert (record["shape"], record["token_budget"]) == ("full", 2_000_000)
    assert Counted.built == [{"api_key": NOT_A_KEY, "base_url": CLAUDE_API_URL}]
    crossing = record["counts"]["crossing"]
    assert crossing is not None and crossing["total"] > 0 and not crossing["partial"]
    assert (record["count_error"], record["clauses"].get("count_failed", 0)) == (None, 0)
    texts = [written_text(tmp_path), json.dumps(record), printed.out + printed.err]
    assert [NOT_A_KEY in text for text in texts] == [False] * 3
    assert harness.markers_in(texts, harness.markers(review)) == 0


def test_a_failed_count_is_recorded_as_its_type_and_status(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import anthropic

    from access_review import FULL

    words = f"refused the count for {NOT_A_KEY}"
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")
    Counted.fails = anthropic.RateLimitError(
        words, response=httpx2.Response(429, request=request), body=None
    )
    try:
        env = keyed_env("graded", LONG_JOB_TOKEN_BUDGET="2000000")  # noqa: S106 - tokens
        (record,), _, _, _ = keyed_session(harness, clean_env, tmp_path, env, FULL)
    finally:
        Counted.fails = None

    assert record["count_error"] == {"type": "RateLimitError", "status": 429}
    assert record["clauses"]["count_failed"] == 1
    assert record["verdict"] == "INCONCLUSIVE"
    assert words not in json.dumps(record)


def test_a_counts_session_runs_its_jobs_in_turn_with_no_kill_and_sums_them(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setattr(harness.rules, "COUNTS_JOBS", 2)
    env = keyed_env("counts", LONG_JOB_TOKEN_BUDGET="1000000", LONG_JOB_JOBS="2")  # noqa: S106
    lines, docker, api, _ = keyed_session(harness, clean_env, tmp_path, env)

    assert [body["token_budget"] for body in api.posted()] == [1_000_000, 1_000_000]
    assert [line["job"] for line in lines] == [1, 2, "total"]
    assert [line["status"] for line in lines[:2]] == ["succeeded", "succeeded"]
    assert [line.get("verdict") for line in lines] == [None, None, None]
    assert lines[2]["counts"]["jobs"] == 2
    assert lines[2]["counts"]["final"] == 2
    assert docker.envs[0]["FAILPOINT"] == ""
    assert not [call for call in docker.calls if call[1:2] in (("kill",), ("start",))]
    on_disk = (tmp_path / "results.jsonl").read_text().splitlines()
    assert [json.loads(line) for line in on_disk] == lines


def test_a_counts_session_runs_the_line_s_jobs_on_the_claude_api_and_one_on_the_fake(
    harness: ModuleType,
) -> None:
    """The fake's reviewer is scripted: more jobs on it would count nothing new."""
    assert harness.counts_jobs("claude") == harness.rules.COUNTS_JOBS == 10
    assert [harness.counts_jobs(provider) for provider in ("fake", "")] == [1, 1]


def test_a_counts_job_whose_wait_would_end_after_the_session_is_not_sent(
    harness: ModuleType, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clean_env.setattr(harness.rules, "COUNTS_JOBS", 3)
    asked: list[float] = []

    def may_submit(ready: float | None, now: float, line: Any) -> bool:
        asked.append(now)
        return len(asked) == 1

    clean_env.setattr(harness, "may_submit", may_submit)
    env = keyed_env("counts", LONG_JOB_TOKEN_BUDGET="1000000", LONG_JOB_JOBS="3")  # noqa: S106
    lines, _, api, _ = keyed_session(harness, clean_env, tmp_path, env)

    assert len(api.posted()) == 1
    assert [line["job"] for line in lines] == [1, 2, "total"]
    assert lines[1]["harness_error"] == "NotStarted"
    assert lines[2]["not_started"] == 2


def test_a_session_drops_the_sdks_own_settings_first(
    harness: ModuleType, clean_env: pytest.MonkeyPatch
) -> None:
    must_fail_env(clean_env)
    clean_env.setenv("ANTHROPIC_LOG", "debug")
    clean_env.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    start_session(harness, clean_env)

    assert not {"ANTHROPIC_LOG", "ANTHROPIC_BASE_URL"} & set(os.environ)


# Every mode runs a test of its own: no session is green with every test skipped


# The provider of a record, by the name the record gives it.
PROVIDERS = {"fake provider": "fake", "Claude API": "claude"}


def run_file_in_mode(
    directory: Path, mode: str, provider: str, verdict: str = "PASS", jobs: int = 1
) -> str:
    """Run the whole run file on a made-up session of `mode`, with the harness's own fixtures.

    A `counts` session has `jobs` jobs, each with its line.
    """
    directory.mkdir()
    (directory / "logs").mkdir()
    (directory / "logs" / "worker.log").write_text("nothing of the job\n")
    graded = {
        "verdict": verdict,
        "clauses": {},
        "floors": {"made": 7, "cleared": 2, "windows": 3, "summaries": 7},
        "provider": provider,
        "failpoint_lines": 1,
        "takeovers": 3,
        "questions": 0,
        "counts": {"kills": [{"landed": True, "window": 2}, {"landed": True, "window": 4}]},
    }
    each = [{"job": job, "status": "succeeded"} for job in range(1, jobs + 1)]
    lines = [*each, {"job": "total"}] if mode == "counts" else [graded]
    (directory / "results.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    (directory / "sample_stack.py").write_text(
        "import json\nfrom types import SimpleNamespace\n\nimport pytest\n\n"
        "from long_job.conftest import pytest_runtest_logreport, record, records\n\n\n"
        '@pytest.fixture(scope="session")\n'
        "def stack():\n"
        f"    lines = {lines!r}\n"
        "    for line in lines:\n"
        "        print('RESULT ' + json.dumps(line))\n"
        "    return SimpleNamespace(\n"
        f"        mode={mode!r}, provider={PROVIDERS[provider]!r},\n"
        f"        directory=__import__('pathlib').Path({str(directory)!r}),\n"
        "        logs=['worker.log'], markers=['MARKER-OF-THE-TEST'], records=lines,\n"
        f"        record=None if {mode!r} == 'counts' else lines[0],\n"
        "    )\n"
    )
    env = {
        **{name: value for name, value in os.environ.items() if not name.startswith("PYTEST_")},
        "PYTHONPATH": os.pathsep.join([str(directory), str(ROOT / "tests")]),
    }
    done = subprocess.run(  # noqa: S603 - the test's own command
        [
            sys.executable,
            "-m",
            "pytest",
            *makefile_flags("long-job"),
            "--noconftest",
            "-p",
            "sample_stack",
            "-p",
            "no:cacheprovider",
            str(RUN_FILE.relative_to(ROOT)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=120,
    )
    return done.stdout + done.stderr


@pytest.mark.parametrize(
    ("mode", "provider", "summary"),
    [
        ("must-fail", "fake provider", "7 passed, 2 skipped"),
        ("must-fail", "Claude API", "6 passed, 3 skipped"),
        ("graded", "Claude API", "7 passed, 2 skipped"),
        ("counts", "Claude API", "2 passed, 7 skipped"),
    ],
)
def test_each_mode_runs_tests_of_its_own_and_the_answers_test_on_every_provider(
    harness: ModuleType, tmp_path: Path, mode: str, provider: str, summary: str
) -> None:
    jobs = harness.counts_jobs(PROVIDERS[provider])
    output = run_file_in_mode(tmp_path / "session", mode, provider, jobs=jobs)

    assert f" {summary} " in output, output[-3000:]
    assert "test_no_answer_is_in_the_record_the_output_or_the_saved_logs PASSED" in output


@pytest.mark.parametrize("verdict", ["VOID", "FAIL_RUNTIME", "NO_CLASS", "FAIL", "INCONCLUSIVE"])
def test_a_graded_session_is_red_on_every_verdict_but_pass(tmp_path: Path, verdict: str) -> None:
    output = run_file_in_mode(tmp_path / "session", "graded", "Claude API", verdict)

    assert " 1 failed, 6 passed, 2 skipped " in output, output[-3000:]
    assert "::test_a_graded_run_is_green_only_on_pass FAILED" in output
    assert "AssertionError: the graded job's verdict is not PASS" in output


# An error inside grading


def grading_fails(*args: Any) -> Any:
    raise KeyError("MARKER-OF-THE-TEST")


def test_an_error_inside_grading_keeps_its_type_only_and_a_crash_beside_it(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "graded", grading_fails)
    found = stack(harness, tmp_path, Docker(), Api())
    seen = harness.Seen(crashed=True)
    record = harness.record_line(found, harness.rules.SHORT, seen, SimpleNamespace())

    # No clause of the job's log is known; the crash the harness saw still is.
    assert (record["verdict"], record["clauses"]) == (
        "FAIL_RUNTIME",
        {"crash": 1, "grading_error": 1},
    )
    assert record["crashed"] is True
    assert (record["grading_error"], record["harness_error"], record["voids"]) == (
        "KeyError",
        None,
        [],
    )
    assert "MARKER-OF-THE-TEST" not in json.dumps(record)


def test_an_error_inside_grading_beside_a_void_of_the_run_is_void_with_both_listed(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "graded", grading_fails)
    alone = harness.record_line(
        stack(harness, tmp_path / "alone", Docker(), Api()),
        harness.rules.SHORT,
        harness.Seen(),
        SimpleNamespace(),
    )
    assert (alone["verdict"], alone["clauses"]) == ("INCONCLUSIVE", {"grading_error": 1})
    failed_verify = harness.record_line(
        stack(harness, tmp_path / "verify", Docker(), Api()),
        harness.rules.SHORT,
        harness.Seen(verify=1),
        SimpleNamespace(),
    )
    assert (failed_verify["verdict"], failed_verify["clauses"]) == (
        "FAIL_RUNTIME",
        {"R2": 1, "grading_error": 1},
    )
    seen = harness.Seen(harness_error="TimeoutError", voids=["harness_error"])
    found = stack(harness, tmp_path, Docker(), Api())
    record = harness.record_line(found, harness.rules.SHORT, seen, SimpleNamespace())

    assert record["verdict"] == "VOID"
    assert record["clauses"] == {"void_harness_error": 1, "grading_error": 1}
    assert (record["grading_error"], record["harness_error"]) == ("KeyError", "TimeoutError")
