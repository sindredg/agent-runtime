"""What a keyed long-job run may start with, and the parts of its harness that need no stack.

The refusals, the kill offsets, the times, the reduced job view, the crash
record and the fake provider's reviewer are functions of `tests/long_job/rules.py`.
Each is checked here with a case that passes and one that breaks only it.
The runtime's own words for a failed job are made by the runtime, not typed
in: the worker loop runs in process against a stubbed provider.
"""

import asyncio
import dataclasses
import importlib.util
import io
import itertools
import json
import logging
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest

from access_review import FULL, SHORT, answer_key, commitment, generate, job_digest, parse_files
from access_review.render import RULES, TASK
from fake_provider.messages import encode_sse, stream_events
from fake_provider.scenario import parse_scenario
from runtime.logs import JsonFormatter
from runtime.provider.messages import AnthropicProvider
from runtime.spec import JobSpec
from runtime.store.jobs import DEADLINE_ERROR, TAKEOVER_LIMIT_ERROR, Job
from runtime.store.status import JobStatus
from runtime.store.steps import Step, StepKind
from runtime.tools.base import ToolRegistry
from runtime.tools.read_file import READ_FILE
from runtime.worker import agent
from runtime.worker.halt import Halt
from runtime.worker.lease import Lease
from runtime.worker.replay import replay

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "long_job"
MODULES = ("rules", "grader", "counting")
PUBLIC_SEED_FILE = ROOT / "tests" / "unit" / "access_review_seed.json"
PUBLIC_SEED = json.loads(PUBLIC_SEED_FILE.read_text())["seed"]
# A seed of the tests' own, and one more. Neither is the committed one.
OTHER_SEED = "00112233445566778899aabbccddeeff"
THIRD_SEED = "ffeeddccbbaa99887766554433221100"
JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947909")
OTHER_JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a194790a")
NOW = datetime.now(UTC)
# Docker's timestamp before each line of `docker logs --timestamps`.
STAMP = "2026-11-26T01:02:03.123456789Z "
# The variable of a job's token budget: a count of tokens, not a secret.
BUDGET = "LONG_JOB_TOKEN_BUDGET"
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "m",
    "content": [{"type": "text", "text": "done"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 9},
}


@pytest.fixture(scope="module")
def rules() -> Iterator[ModuleType]:
    """The suite's rules, loaded by path. A job is generated once per seed and shape."""
    spec = importlib.util.spec_from_file_location("long_job_rules", SUITE / "rules.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules["long_job_rules"] = module
    spec.loader.exec_module(module)
    made: dict[tuple[bytes, str], Any] = {}
    checked: dict[tuple[bytes, str], list[str]] = {}
    real_generate, real_problems = module.generate, module.problems

    def generate_once(seed: bytes, shape: Any) -> Any:
        if (seed, shape.name) not in made:
            made[seed, shape.name] = real_generate(seed, shape)
        return made[seed, shape.name]

    def problems_once(review: Any) -> list[str]:
        # Only a review that `generate_once` keeps is checked once. Any other,
        # such as one a test changed, is checked each time.
        key = next((key for key, kept in made.items() if kept is review), None)
        if key is None:
            found: list[str] = real_problems(review)
            return found
        if key not in checked:
            checked[key] = real_problems(review)
        return checked[key]

    module.generate, module.problems = generate_once, problems_once  # type: ignore[attr-defined]
    yield module
    module.generate, module.problems = real_generate, real_problems  # type: ignore[attr-defined]


# The modules load alone


@pytest.mark.parametrize("name", MODULES)
def test_a_pure_module_loads_alone_and_without_the_provider_sdk(name: str) -> None:
    """Loaded by path in a new interpreter: no other module of the suite, and no SDK."""
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('alone', {str(SUITE / (name + '.py'))!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['alone'] = module\n"
        "spec.loader.exec_module(module)\n"
        "print(sorted(n for n in sys.modules if n.split('.')[0] in ('anthropic', 'runtime')))\n"
    )
    ran = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, cwd=ROOT
    )

    assert (ran.returncode, ran.stdout.strip()) == (0, "[]")


def test_no_pure_module_names_another_or_the_sdk() -> None:
    for name in MODULES:
        text = (SUITE / f"{name}.py").read_text()
        others = [other for other in MODULES if other != name]
        imports = [line for line in text.splitlines() if line.startswith(("import ", "from "))]
        assert imports
        assert not [line for line in imports if "anthropic" in line or "long_job" in line]
        assert not [line for line in imports for other in others if other in line.split()]


# The line as it ships


def test_the_line_ships_fixed_with_its_bound_and_number_of_jobs(rules: ModuleType) -> None:
    """Fixed on 2026-11-27: ten counts jobs of the short cap, and the must-fail pair fits too."""
    assert rules.LINE_FIXED == "2026-11-27"
    assert rules.COUNTS_JOBS == 10
    assert rules.LONG_KEYED_TOKEN_BOUND == 15_000_000
    assert rules.COUNTS_JOBS * rules.SHORT.budget_cap == rules.LONG_KEYED_TOKEN_BOUND
    assert rules.MUST_FAIL_JOBS * rules.SHORT.budget_cap <= rules.LONG_KEYED_TOKEN_BOUND
    assert json.loads((SUITE / "commitment.json").read_text()) == {"full": None}


def test_the_numbers_of_the_line(rules: ModuleType) -> None:
    full, short = rules.FULL, rules.SHORT
    assert rules.MODEL == "claude-haiku-5-5"
    assert rules.MAX_TOKENS == 32_000
    assert (full.files, short.files) == (FULL.files, SHORT.files) == (293, 36)
    assert rules.FILE_BYTES == 16_384
    assert (full.trigger, short.trigger) == (80_000, 20_000)
    assert (full.kill_after, short.kill_after) == ((3, 7), (2, 4))
    assert (full.floor_made, full.floor_cleared, full.floor_windows) == (6, 2, 3)
    assert (short.floor_made, short.floor_cleared, short.floor_windows) == (6, 2, 3)
    assert (full.floor_summaries, short.floor_summaries) == (0, 6)
    assert (full.crossing, short.crossing) == (True, False)
    assert (full.takeovers, short.takeovers) == (3, 3)
    assert (rules.CROSSING_FLOOR, rules.CORPUS_FLOOR) == (1_000_000, 1_200_000)
    assert rules.CHUNK_BYTES == 524_288
    assert (full.budget_cap, short.budget_cap) == (20_000_000, 1_500_000)
    assert {mode: line.name for mode, line in rules.MODES.items()} == {
        "counts": "short",
        "must-fail": "short",
        "graded": "full",
    }


# The times


def test_a_full_job_ends_inside_its_session_and_the_session_inside_the_workflow_job(
    rules: ModuleType,
) -> None:
    six_hours = 6 * 3600
    assert rules.FULL.timeout_seconds == 18_600
    assert 18_600 < rules.HARNESS_MINUTES * 60 == 320 * 60
    assert 320 * 60 < rules.WORKFLOW_MINUTES * 60 == 350 * 60 < six_hours
    assert rules.FULL.job_minutes == rules.HARNESS_MINUTES


def test_a_short_job_is_started_only_while_its_wait_fits_the_session(rules: ModuleType) -> None:
    short = rules.SHORT
    assert (short.timeout_seconds, short.job_minutes) == (1_800, 40)
    assert short.timeout_seconds < short.job_minutes * 60
    # Ten jobs one after another, each taking its whole wait: eight start.
    started = [n for n in range(10) if rules.may_start(n * short.job_minutes * 60, short)]
    assert started == list(range(8))
    assert rules.may_start(280 * 60, short)
    assert not rules.may_start(280 * 60 + 1, short)
    assert rules.may_start(0, rules.FULL)
    assert not rules.may_start(1, rules.FULL)


def test_two_short_sessions_and_two_builds_fit_the_workflow_job(rules: ModuleType) -> None:
    sessions = rules.MUST_FAIL_JOBS * rules.SHORT.job_minutes
    builds = rules.MUST_FAIL_JOBS * rules.BUILD_MINUTES
    assert (rules.MUST_FAIL_JOBS, sessions, builds) == (2, 80, 60)
    assert sessions + builds < rules.WORKFLOW_MINUTES


# The refusals


@pytest.fixture
def open_line(rules: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """The line as it would be once fixed: a date, a bound and a number of jobs."""
    monkeypatch.setattr(rules, "LINE_FIXED", "2026-01-01")
    monkeypatch.setattr(rules, "LONG_KEYED_TOKEN_BOUND", 3_000_000)
    monkeypatch.setattr(rules, "COUNTS_JOBS", 2)


def checkout(directory: Path, committed: str | None = None) -> Path:
    """A checkout with the committed seed file and a commitment file."""
    (directory / "tests" / "unit").mkdir(parents=True)
    (directory / "tests" / "long_job").mkdir(parents=True)
    (directory / "tests" / "unit" / "access_review_seed.json").write_bytes(
        PUBLIC_SEED_FILE.read_bytes()
    )
    (directory / "tests" / "long_job" / "commitment.json").write_text(
        json.dumps({"full": committed})
    )
    return directory


def committed_to(seed: str) -> str:
    """The commitment to the full shape's job of `seed`."""
    review = generate(bytes.fromhex(seed), FULL)
    return commitment(bytes.fromhex(seed), FULL, answer_key(review), job_digest(review))


@pytest.fixture(scope="module")
def other_commitment() -> str:
    return committed_to(OTHER_SEED)


def run_env(mode: str, **changes: str | None) -> dict[str, str]:
    """A keyed run nothing refuses once the line is open. None takes a variable out."""
    env = {"LONG_JOB_PROVIDER": "claude", "LONG_JOB_MODE": mode, "LONG_JOB_SEED": OTHER_SEED}
    if mode == "counts":
        env |= {BUDGET: "1500000", "LONG_JOB_JOBS": "2"}
    if mode == "graded":
        env[BUDGET] = "20000000"
    for name, value in changes.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env


@pytest.mark.usefixtures("open_line")
def test_an_open_run_has_no_refusal(
    rules: ModuleType, tmp_path: Path, other_commitment: str
) -> None:
    """The control of every case below: each of them changes one thing from here."""
    short = checkout(tmp_path / "short")
    full = checkout(tmp_path / "full", other_commitment)

    assert rules.refusals(run_env("counts"), short) == []
    assert rules.refusals(run_env("must-fail"), short) == []
    assert rules.refusals(run_env("graded"), full) == []


def test_the_shipped_line_refuses_no_run_that_sets_its_numbers(
    rules: ModuleType, tmp_path: Path, other_commitment: str
) -> None:
    """The date, the bound and the number of jobs as they ship. The run gives its own."""
    short = checkout(tmp_path / "short")
    full = checkout(tmp_path / "full", other_commitment)
    counts = run_env("counts", LONG_JOB_JOBS="10")

    assert rules.refusals(counts, short) == []
    assert rules.refusals(run_env("must-fail"), short) == []
    assert rules.refusals(run_env("graded"), full) == []
    # One job more than the line fixes is refused.
    assert rules.refusals(run_env("counts", LONG_JOB_JOBS="11"), short) == [rules.JOBS_NOT_FIXED]


def test_a_line_with_no_date_refuses_every_keyed_run(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_commitment: str
) -> None:
    """`LINE_FIXED` unset. Everything else is open."""
    monkeypatch.setattr(rules, "LINE_FIXED", None)
    monkeypatch.setattr(rules, "LONG_KEYED_TOKEN_BOUND", 3_000_000)
    monkeypatch.setattr(rules, "COUNTS_JOBS", 2)
    short = checkout(tmp_path / "short")
    full = checkout(tmp_path / "full", other_commitment)

    assert rules.refusals(run_env("counts"), short) == [rules.PROPOSED]
    assert rules.refusals(run_env("must-fail"), short) == [rules.PROPOSED]
    assert rules.refusals(run_env("graded"), full) == [rules.PROPOSED]


def test_on_the_fake_provider_nothing_is_refused(rules: ModuleType, tmp_path: Path) -> None:
    """The line as it ships, no seed, no budget, a mode that does not exist, an empty checkout."""
    for provider in (None, "", "fake"):
        env = run_env("no-such-mode", LONG_JOB_PROVIDER=provider, LONG_JOB_SEED=None)
        assert rules.refusals(env, tmp_path) == []
    # The same run on the Claude API is refused, so the cases above show the provider's part.
    assert rules.refusals(run_env("no-such-mode", LONG_JOB_SEED=None), tmp_path) == [rules.BAD_MODE]


@pytest.mark.parametrize("provider", ["Claude", "anthropic", "fake ", "claude-api", "none"])
def test_a_provider_that_is_neither_the_fake_nor_claude_is_refused(
    rules: ModuleType, tmp_path: Path, provider: str
) -> None:
    """A misspelt provider is not taken for the fake, whatever else the run sets."""
    env = run_env("no-such-mode", LONG_JOB_PROVIDER=provider, LONG_JOB_SEED=None)

    assert rules.refusals(env, tmp_path) == [rules.BAD_PROVIDER]


@pytest.mark.usefixtures("open_line")
@pytest.mark.parametrize("mode", [None, "", "Graded", "must_fail", "count"])
def test_an_unknown_mode_is_refused(rules: ModuleType, tmp_path: Path, mode: str | None) -> None:
    env = run_env("counts", LONG_JOB_MODE=mode)

    assert rules.refusals(env, checkout(tmp_path)) == [rules.BAD_MODE]


@pytest.mark.usefixtures("open_line")
def test_a_run_with_no_token_budget_is_refused(rules: ModuleType, tmp_path: Path) -> None:
    root = checkout(tmp_path)

    for budget in (None, ""):
        env = run_env("counts", **{BUDGET: budget})
        assert rules.refusals(env, root) == [rules.NO_BUDGET]


@pytest.mark.usefixtures("open_line")
@pytest.mark.parametrize(
    "budget",
    [
        "0",
        "abc",
        "1e6",
        "-1",
        "15 00",
        " 1500000",
        "1500000\n",
        "1_500_000",
        "1500001",
        "\u0661\u0665",
    ],
)
def test_a_token_budget_that_is_not_a_number_inside_the_cap_is_refused(
    rules: ModuleType, tmp_path: Path, budget: str
) -> None:
    env = run_env("counts", **{BUDGET: budget})

    assert rules.refusals(env, checkout(tmp_path)) == [rules.BAD_BUDGET]


@pytest.mark.usefixtures("open_line")
def test_the_cap_of_a_token_budget_is_the_shapes(
    rules: ModuleType, tmp_path: Path, other_commitment: str
) -> None:
    """20,000,000 opens a full job and 1 more does not; `run_env` gives a short job its cap."""
    root = checkout(tmp_path, other_commitment)
    over = run_env("graded", **{BUDGET: "20000001"})

    assert rules.refusals(over, root) == [rules.BAD_BUDGET]
    assert rules.refusals(run_env("graded", **{BUDGET: "1"}), root) == []


@pytest.mark.usefixtures("open_line")
def test_a_must_fail_run_takes_the_short_cap_and_refuses_a_given_budget(
    rules: ModuleType, tmp_path: Path
) -> None:
    root = checkout(tmp_path)

    assert rules.refusals(run_env("must-fail"), root) == []
    for budget in ("1500000", "1", "abc"):
        env = run_env("must-fail", **{BUDGET: budget})
        assert rules.refusals(env, root) == [rules.BUDGET_GIVEN]


@pytest.mark.usefixtures("open_line")
@pytest.mark.parametrize("seed", [None, "", "not a seed", OTHER_SEED[:30], OTHER_SEED + "0"])
def test_a_run_with_no_seed_is_refused(rules: ModuleType, tmp_path: Path, seed: str | None) -> None:
    env = run_env("must-fail", LONG_JOB_SEED=seed)

    assert rules.refusals(env, checkout(tmp_path)) == [rules.NO_SEED]


@pytest.mark.usefixtures("open_line")
@pytest.mark.parametrize("seed", [PUBLIC_SEED, PUBLIC_SEED.upper(), f"  {PUBLIC_SEED}\n"])
def test_the_committed_public_seed_is_refused_however_it_is_written(
    rules: ModuleType, tmp_path: Path, seed: str
) -> None:
    env = run_env("must-fail", LONG_JOB_SEED=seed)

    assert rules.refusals(env, checkout(tmp_path)) == [rules.PUBLIC_SEED]


@pytest.mark.usefixtures("open_line")
def test_a_seed_is_refused_when_the_public_seed_cannot_be_read(
    rules: ModuleType, tmp_path: Path
) -> None:
    root = checkout(tmp_path)
    (root / "tests" / "unit" / "access_review_seed.json").unlink()

    assert rules.refusals(run_env("must-fail"), root) == [rules.PUBLIC_SEED]


@pytest.mark.usefixtures("open_line")
def test_a_graded_run_needs_the_commitment_of_its_seed(rules: ModuleType, tmp_path: Path) -> None:
    unset = checkout(tmp_path / "unset")
    another = checkout(tmp_path / "another", committed_to(THIRD_SEED))
    broken = checkout(tmp_path / "broken")
    (broken / "tests" / "long_job" / "commitment.json").write_text("{")
    missing = checkout(tmp_path / "missing")
    (missing / "tests" / "long_job" / "commitment.json").unlink()

    for root in (unset, another, broken, missing):
        assert rules.refusals(run_env("graded"), root) == [rules.NOT_COMMITTED]


@pytest.mark.usefixtures("open_line")
@pytest.mark.parametrize("mode", ["counts", "must-fail"])
def test_a_short_run_refuses_the_seed_the_commitment_is_made_to(
    rules: ModuleType, tmp_path: Path, other_commitment: str, mode: str
) -> None:
    root = checkout(tmp_path, other_commitment)

    assert rules.refusals(run_env(mode), root) == [rules.COMMITTED]
    assert rules.refusals(run_env(mode, LONG_JOB_SEED=THIRD_SEED), root) == []


@pytest.mark.usefixtures("open_line")
def test_a_seed_whose_job_fails_a_check_is_refused(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generator is patched to give a job with one grant line gone from a file."""
    real = rules.generate

    def one_grant_less(seed: bytes, shape: Any) -> Any:
        review = real(seed, shape)
        path = "access/0001.txt"
        lines = review.files[path].split("\n")
        del lines[lines.index("[grants]") + 1]
        return dataclasses.replace(review, files={**review.files, path: "\n".join(lines)})

    monkeypatch.setattr(rules, "generate", one_grant_less)
    assert rules.refusals(run_env("must-fail"), checkout(tmp_path)) == [rules.CHECKS]


@pytest.mark.usefixtures("open_line")
def test_a_seed_whose_job_cannot_be_made_is_refused_with_the_same_sentence(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(seed: bytes, shape: Any) -> Any:
        raise RuntimeError(f"seed {seed.hex()}")

    monkeypatch.setattr(rules, "generate", broken)
    assert rules.refusals(run_env("must-fail"), checkout(tmp_path)) == [rules.CHECKS]


@pytest.mark.usefixtures("open_line")
def test_a_short_run_is_refused_without_the_token_bound_or_over_it(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two jobs of 1,500,000 tokens are 3,000,000: the bound of `open_line`, to the token."""
    root = checkout(tmp_path)
    for bound in (None, 2_999_999):
        monkeypatch.setattr(rules, "LONG_KEYED_TOKEN_BOUND", bound)
        assert rules.refusals(run_env("must-fail"), root) == [rules.NO_BOUND]
        assert rules.refusals(run_env("counts"), root) == [rules.NO_BOUND]
    # A smaller budget for each job fits the smaller bound.
    assert rules.refusals(run_env("counts", **{BUDGET: "1499999"}), root) == []


@pytest.mark.usefixtures("open_line")
def test_a_graded_run_is_not_held_by_the_bound_of_short_runs(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, other_commitment: str
) -> None:
    monkeypatch.setattr(rules, "LONG_KEYED_TOKEN_BOUND", None)
    monkeypatch.setattr(rules, "COUNTS_JOBS", None)

    assert rules.refusals(run_env("graded"), checkout(tmp_path, other_commitment)) == []


@pytest.mark.usefixtures("open_line")
def test_a_counts_run_needs_the_fixed_number_of_jobs(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = checkout(tmp_path)
    for jobs in (None, "", "1", "3", "02", "2 "):
        env = run_env("counts", LONG_JOB_JOBS=jobs)
        assert rules.refusals(env, root) == [rules.JOBS_NOT_FIXED]
    monkeypatch.setattr(rules, "COUNTS_JOBS", None)
    assert rules.refusals(run_env("counts"), root) == [rules.JOBS_NOT_FIXED]
    # A must-fail run has its own number of jobs and reads neither.
    assert rules.refusals(run_env("must-fail", LONG_JOB_JOBS="9"), root) == []


def test_no_refusal_quotes_a_value(
    rules: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every refusal at once, from values that would be seen in a sentence. The line is open."""
    for name in ("LINE_FIXED", "LONG_KEYED_TOKEN_BOUND", "COUNTS_JOBS"):
        monkeypatch.setattr(rules, name, None)
    sentences = {
        getattr(rules, name)
        for name in (
            "BAD_PROVIDER",
            "PROPOSED",
            "BAD_MODE",
            "NO_BUDGET",
            "BAD_BUDGET",
            "BUDGET_GIVEN",
            "JOBS_NOT_FIXED",
            "NO_BOUND",
            "NO_SEED",
            "PUBLIC_SEED",
            "NOT_COMMITTED",
            "COMMITTED",
            "CHECKS",
        )
    }
    values = ("987654321", "77", "zz-mode", "0123456789abcdef0123456789abcdeX", PUBLIC_SEED)
    budget, jobs, mode, bad_seed, seed = values
    runs = [
        run_env("counts", **{BUDGET: budget}, LONG_JOB_JOBS=jobs, LONG_JOB_SEED=seed),
        run_env("counts", LONG_JOB_SEED=bad_seed),
        run_env("must-fail", **{BUDGET: budget}, LONG_JOB_SEED=seed.upper()),
        run_env("graded", LONG_JOB_SEED=seed),
        run_env(mode),
        run_env("counts", LONG_JOB_PROVIDER=mode),
    ]
    root = checkout(tmp_path)
    found = [sentence for env in runs for sentence in rules.refusals(env, root)]

    assert len(sentences) == 13
    assert set(found) <= sentences
    assert len(set(found)) >= 10
    for sentence in sentences:
        assert not [value for value in values if value.lower() in sentence.lower()]
        assert OTHER_SEED not in sentence


# The kill offsets


def test_the_offsets_of_the_committed_seed(rules: ModuleType) -> None:
    seed = bytes.fromhex(PUBLIC_SEED)
    full = [rules.offset(seed, "full", i) for i in (2, 3)]
    short = [rules.offset(seed, "short", i) for i in (2, 3)]

    assert (full, short) == ([1, 10], [8, 8])
    assert full != short


def test_every_offset_is_from_1_to_20_and_each_is_drawn(rules: ModuleType) -> None:
    drawn = {
        rules.offset(n.to_bytes(16, "big"), shape, i)
        for n in range(200)
        for shape in ("full", "short")
        for i in (2, 3)
    }

    assert drawn == set(range(1, 21))


def test_an_offset_is_drawn_again_when_its_value_is_past_the_last_whole_range(
    rules: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stub digest is past the limit for the first draw and 7 for the second."""
    seen: list[bytes] = []
    limit = (2**64 // 20) * 20

    class Digest:
        def __init__(self, value: int) -> None:
            self.value = value

        def digest(self) -> bytes:
            return self.value.to_bytes(8, "big") + bytes(24)

    class Hashes:
        @staticmethod
        def sha256(block: bytes) -> Digest:
            seen.append(block)
            return Digest(limit if len(seen) == 1 else 7)

    monkeypatch.setattr(rules, "hashlib", Hashes)
    assert rules.offset(b"\x01\x02", "short", 3) == 8
    assert seen == [
        b"long-job-kill\x00short\x00\x03\x00\x01\x02",
        b"long-job-kill\x00short\x00\x03\x01\x01\x02",
    ]


def test_a_value_just_under_the_limit_is_kept(
    rules: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = (2**64 // 20) * 20

    class Digest:
        def digest(self) -> bytes:
            return (limit - 1).to_bytes(8, "big") + bytes(24)

    class Hashes:
        @staticmethod
        def sha256(block: bytes) -> Digest:
            return Digest()

    monkeypatch.setattr(rules, "hashlib", Hashes)
    assert rules.offset(b"", "full", 2) == 20


# The reduced job view


def view(**changes: Any) -> dict[str, Any]:
    """A job as `GET /jobs/{id}` returns it, with a spec and an error that hold a marker."""
    return {
        "id": str(JOB),
        "status": "failed",
        "spec": {"prompt": "MARKER in the prompt", "files": {"index.txt": "MARKER"}},
        "token_budget": 1_500_000,
        "tokens_used": 829_211,
        "deadline": "2026-11-26T02:00:00Z",
        "not_before": None,
        "lease_owner": "0123456789ab-1",
        "lease_expires_at": "2026-11-26T01:00:30Z",
        "lease_epoch": 4,
        "takeovers": 3,
        "cancel_requested": False,
        "error": "ValueError: MARKER in the error",
        "created_at": "2026-11-26T01:00:00Z",
        **changes,
    }


def test_a_reduced_job_has_no_spec_and_no_error_text(rules: ModuleType) -> None:
    reduced = rules.reduced(view())

    assert reduced == {
        "status": "failed",
        "takeovers": 3,
        "lease_owner": "0123456789ab-1",
        "lease_expires_at": "2026-11-26T01:00:30Z",
        "tokens_used": 829_211,
        "deadline": "2026-11-26T02:00:00Z",
        "error": "other",
        "question": None,
    }
    assert "MARKER" not in json.dumps(reduced)


def test_a_job_of_the_list_is_reduced_the_same_way(rules: ModuleType) -> None:
    """The list has no lease and no takeovers, and names the open question."""
    listed = {
        "id": str(JOB),
        "status": "needs_approval",
        "token_budget": 1_500_000,
        "tokens_used": 5,
        "deadline": "2026-11-26T02:00:00Z",
        "error": "model call failed: provider answered 400: MARKER",
        "created_at": "2026-11-26T01:00:00Z",
        "question": {"seq": 41, "tool": "MARKER", "reason": "MARKER"},
    }
    reduced = rules.reduced(listed)

    assert reduced == {
        "status": "needs_approval",
        "takeovers": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "tokens_used": 5,
        "deadline": "2026-11-26T02:00:00Z",
        "error": "provider_error",
        "question": 41,
    }
    assert "MARKER" not in json.dumps(reduced)


def test_every_error_class_is_a_fixed_name(rules: ModuleType) -> None:
    texts = [None, "", "MARKER", 7, {"error": "MARKER"}, DEADLINE_ERROR + " MARKER"]

    assert rules.error_class(None) == "none"
    assert len(set(rules.ERROR_CLASSES)) == len(rules.ERROR_CLASSES) == 18
    assert {rules.error_class(text) for text in texts} <= {"none", "other"}


# The runtime's own words for a failed job


def job(spec: dict[str, Any], budget: int) -> Job:
    return Job(
        id=JOB,
        status=JobStatus.RUNNING,
        spec=spec,
        token_budget=budget,
        tokens_used=0,
        deadline=NOW + timedelta(hours=1),
        not_before=None,
        lease_owner="w",
        lease_expires_at=None,
        lease_epoch=1,
        takeovers=0,
        cancel_requested=False,
        error=None,
        created_at=NOW,
    )


class Writer:
    """The step log in memory, as the worker's own writer folds it into the state."""

    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)
        self.tokens_added = 0
        self.state: Any = None

    async def append(self, kind: StepKind, payload: dict[str, Any], *, tokens: int = 0) -> Step:
        written = Step(JOB, len(self.steps) + 1, kind, json.loads(json.dumps(payload)), 1, NOW)
        self.steps.append(written)
        self.tokens_added += tokens
        if self.state is not None:
            self.state.apply(written)
        return written


def failure(
    handler: Callable[[httpx2.Request], httpx2.Response],
    tmp_path: Path,
    *,
    steps: Sequence[Step] = (),
    needs_approval: Sequence[str] = (),
    budget: int = 1_000_000,
) -> str:
    """The error the worker loop fails a job with when the provider answers as `handler` does."""
    spec: dict[str, Any] = {"prompt": "go", "model": "m"}
    if needs_approval:
        spec["needs_approval"] = list(needs_approval)
    settings = agent.AgentSettings(
        default_model="m", max_tokens=1_000, workspace_root=tmp_path, summary_trigger=None
    )

    async def go() -> str:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        provider = AnthropicProvider(api_key="k", base_url="http://provider", http_client=http)
        try:
            await agent._loop(
                Writer(steps),  # type: ignore[arg-type]
                provider,
                ToolRegistry([READ_FILE]),
                job(spec, budget),
                JobSpec.model_validate(spec),
                settings,
                tmp_path,
                Lease(JOB, 1, timedelta(seconds=60), claimed_at=time.monotonic()),
                list(steps),
                asyncio.Event(),
                Halt(JOB),
            )
        except agent._JobFailed as failed:
            return str(failed)
        finally:
            await provider.aclose()
        raise AssertionError("the job did not fail")

    return asyncio.run(go())


def answered(status: int, kind: str, message: str = "nope", **details: str) -> httpx2.Response:
    """An error answer of the Messages API."""
    detail: dict[str, object] = {"type": kind, "message": message}
    if details:
        detail["details"] = details
    return httpx2.Response(status, json={"type": "error", "error": detail})


def streamed(*, stop_reason: str = "end_turn", error: dict[str, object] | None = None) -> Any:
    """A streamed answer: a whole message, or one that breaks off with an `error` event."""
    events = [
        encode_sse(event, data)
        for event, data in stream_events({**MESSAGE, "stop_reason": stop_reason})
    ]
    if error is not None:
        events = [*events[:3], encode_sse("error", {"type": "error", "error": error})]
    return httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=b"".join(events)
    )


def waits(count: int) -> list[Step]:
    """A log in which the job already waited `count` times for a provider error."""
    waiting = {"to": "waiting", "cause": "provider_error"}
    return [Step(JOB, n + 1, StepKind.STATUS_CHANGED, waiting, 1, NOW) for n in range(count)]


SPEND_CAP = {"error_code": "enforced_spend_limit_reached"}
RATE_LIMIT_IN_STREAM: dict[str, object] = {"type": "rate_limit_error", "message": "nope"}


def test_the_stores_two_texts_are_the_runtimes(rules: ModuleType) -> None:
    assert rules.DEADLINE_ERROR == DEADLINE_ERROR
    assert rules.TAKEOVER_CAP_ERROR == TAKEOVER_LIMIT_ERROR
    assert rules.error_class(DEADLINE_ERROR) == "deadline"
    assert rules.error_class(TAKEOVER_LIMIT_ERROR) == "takeover_cap"


def test_a_spend_cap_is_told_from_every_other_provider_error(
    rules: ModuleType, tmp_path: Path
) -> None:
    """Each text is the worker loop's, from an answer of the stubbed provider."""
    capped = failure(lambda r: answered(429, "rate_limit_error", **SPEND_CAP), tmp_path)
    quoting = failure(
        lambda r: answered(400, "invalid_request_error", "provider answered 429: no"), tmp_path
    )
    in_stream = failure(
        lambda r: streamed(error={**RATE_LIMIT_IN_STREAM, "details": SPEND_CAP}), tmp_path
    )
    other_in_stream = failure(
        lambda r: streamed(error={"type": "invalid_request_error", "message": "nope"}), tmp_path
    )

    assert capped.startswith("model call failed: provider answered 429: ")
    assert "enforced_spend_limit_reached" in capped
    assert rules.error_class(capped) == "spend_cap"
    assert quoting.startswith("model call failed: provider answered 400: ")
    assert "429" in quoting
    assert rules.error_class(quoting) == "provider_error"
    assert rules.error_class(in_stream) == "stream_spend_cap"
    assert rules.error_class(other_in_stream) == "provider_error"


def test_a_provider_error_at_the_cap_is_its_own_class_whatever_it_quotes(
    rules: ModuleType, tmp_path: Path
) -> None:
    """Ten waits in the log, then one more error: the worker gives up at the cap."""
    server = failure(lambda r: answered(500, "api_error"), tmp_path, steps=waits(10))
    rate = failure(lambda r: streamed(error=RATE_LIMIT_IN_STREAM), tmp_path, steps=waits(10))

    assert "provider answered 500" in server
    assert "rate_limit_error" in rate
    for text in (server, rate):
        assert text.startswith("model call failed: gave up at the cap of 10 provider errors")
        assert rules.error_class(text) == "provider_error_cap"


def test_the_other_ways_the_worker_loop_fails_a_job_each_have_a_class(
    rules: ModuleType, tmp_path: Path
) -> None:
    def never(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no call is made")

    summary_request = Step(JOB, 1, StepKind.MODEL_REQUEST, {"summary": True}, 1, NOW)

    def after_summary(payload: dict[str, Any]) -> str:
        stored = {"content": [{"type": "text", "text": "kept"}], "stop_reason": "end_turn"}
        summary = Step(JOB, 2, StepKind.SUMMARY, {**stored, **payload}, 1, NOW)
        return failure(never, tmp_path, steps=[summary_request, summary])

    made = {
        "not_offered": failure(never, tmp_path, needs_approval=["run_shell"]),
        "budget": failure(never, tmp_path, budget=1),
        "max_tokens": failure(lambda r: streamed(stop_reason="max_tokens"), tmp_path),
        "refusal": failure(lambda r: streamed(stop_reason="refusal"), tmp_path),
        "stop_reason": failure(lambda r: streamed(stop_reason="pause_turn"), tmp_path),
        "message_mismatch": after_summary({"message": {"role": "user", "content": "other"}}),
        "summary_problem": after_summary({"problem": "it has no text"}),
        "repeat": agent._repeat_error("read_file", ToolRegistry([READ_FILE])),
    }
    opened = replay("go", [])
    opened.opening_input = 9
    oversize = opened.oversize(5)
    assert oversize is not None
    made["oversize"] = oversize
    response = Step(
        JOB, 1, StepKind.MODEL_RESPONSE, {"content": [], "stop_reason": "", "problem": "p"}, 1, NOW
    )
    made["response_problem"] = failure(never, tmp_path, steps=[response])

    assert {name: rules.error_class(text) for name, text in made.items()} == {
        name: name for name in made
    }
    assert made["message_mismatch"] == rules.MESSAGE_MISMATCH_ERROR
    # The rest: no error, an unknown one, the store's two and the provider's four above.
    assert set(rules.ERROR_CLASSES) - set(made) == {
        "none",
        "other",
        "deadline",
        "takeover_cap",
        "spend_cap",
        "stream_spend_cap",
        "provider_error_cap",
        "provider_error",
    }


# The crash record


def logged(message: str, job_id: uuid.UUID, *, crash: bool = False, **extra: Any) -> str:
    """One line of a worker's log, written by the runtime's formatter, as Docker saves it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("long_job_harness_test")
    log.addHandler(handler)
    log.propagate = False
    log.setLevel(logging.INFO)
    try:
        if crash:
            try:
                raise ValueError(f"job crashed for {OTHER_JOB}")
            except ValueError:
                log.exception(message, extra={"job_id": job_id, **extra})
        else:
            log.info(message, extra={"job_id": job_id, **extra})
    finally:
        log.removeHandler(handler)
    return STAMP + stream.getvalue()


def test_the_crash_record_of_a_job_is_found_in_a_worker_log(rules: ModuleType) -> None:
    crash = logged("job crashed", JOB, crash=True)
    log = logged("step", JOB, seq=3, kind="tool_result") + crash + "not JSON at all\n\n"

    assert '"exception"' in crash
    assert rules.crashed(log, str(JOB))
    # Without Docker's timestamp too.
    assert rules.crashed(crash.removeprefix(STAMP), str(JOB))


def test_another_jobs_crash_is_not_this_jobs(rules: ModuleType) -> None:
    """The traceback of the other job's crash names this job; its own id does not."""
    crash = logged("job crashed", JOB, crash=True)

    assert str(OTHER_JOB) in crash
    assert not rules.crashed(crash, str(OTHER_JOB))
    assert rules.crashed(crash, str(JOB))


def test_the_words_of_the_crash_record_inside_another_record_show_nothing(
    rules: ModuleType,
) -> None:
    finished = logged(
        "job finished", JOB, status="failed", error=f'job crashed "job_id": "{JOB}" job crashed'
    )
    listed = STAMP + json.dumps(["job crashed", str(JOB)]) + "\n"

    assert "job crashed" in finished
    assert str(JOB) in finished
    assert not rules.crashed(finished + listed, str(JOB))
    assert not rules.crashed("", str(JOB))


# The text that drops the notices


@pytest.mark.parametrize("shape", [FULL, SHORT], ids=["full", "short"])
def test_the_text_that_drops_the_notices_keeps_the_task_and_holds_no_answer(
    rules: ModuleType, shape: Any
) -> None:
    review = rules.generate(bytes.fromhex(PUBLIC_SEED), shape)
    parts = review.prompt.split("\n\n")
    grants = {grant.id for file in parse_files(review.files).files for grant in file.grants}
    words = set(rules.NOTICES_DROPPED.replace("\n", " ").split(" "))

    assert len(parts) == 4
    assert parts[:2] == [TASK, RULES]
    assert rules.NOTICES_DROPPED.startswith(TASK + "\n\n" + RULES + "\n\n")
    assert rules.NOTICES_DROPPED.count("\n\n") == 2
    assert not rules.NOTICES_DROPPED.endswith("\n")
    assert len(review.notices) == 16
    assert not [notice for notice in review.notices if notice.text in rules.NOTICES_DROPPED]
    assert len(grants) in (3_862, 31_180)
    assert not [grant for grant in grants if grant in rules.NOTICES_DROPPED]
    assert not words & grants
    assert review.reference_code not in rules.NOTICES_DROPPED
    assert "reference code" not in rules.NOTICES_DROPPED


def test_the_last_line_of_that_text(rules: ModuleType) -> None:
    assert rules.NOTICES_DROPPED.split("\n\n")[2] == (
        "Nothing is recorded as read. Continue through every file that index.txt lists,"
        " starting with the first."
    )


# The fake provider's reviewer


@pytest.fixture(scope="module")
def short_review(rules: ModuleType) -> Any:
    return rules.generate(bytes.fromhex(PUBLIC_SEED), SHORT)


def calls(turn: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    return [(b["name"], b["input"]) for b in turn["content"] if b["type"] == "tool_use"]


def test_the_scripted_reviewer_is_a_scenario_the_fake_provider_takes(
    rules: ModuleType, short_review: Any
) -> None:
    scenario = rules.scripted_reviewer(short_review)
    parsed = parse_scenario(json.dumps(scenario))

    assert parsed.summary is not None
    assert parsed.summary.text == rules.SUMMARY_TEXT
    assert parsed.context_window is None
    assert not [turn for turn in parsed.turns if turn.delay_seconds]
    assert parsed.turns[-1].resolved_stop_reason == "end_turn"
    assert scenario["turns"][-1]["content"] == [{"type": "text", "text": rules.LAST_TURN}]


def test_the_scripted_reviewer_reads_every_file_in_order_one_a_turn(
    rules: ModuleType, short_review: Any
) -> None:
    turns = rules.scripted_reviewer(short_review)["turns"]
    reads = [calls(turn) for turn in turns if calls(turn) and calls(turn)[0][0] == "read_file"]

    assert [len(read) for read in reads] == [1] * 37
    assert [read[0][1]["path"] for read in reads] == list(short_review.files)
    assert calls(turns[0]) == [("read_file", {"path": "index.txt"})]


def test_the_scripted_reviewer_opens_one_issue_for_each_violation_after_its_file(
    rules: ModuleType, short_review: Any
) -> None:
    turns = rules.scripted_reviewer(short_review)["turns"]
    key = answer_key(short_review)
    code = short_review.reference_code
    titles: list[str] = []
    read: dict[str, str] = {}
    for before, turn in itertools.pairwise(turns):
        if calls(before) and calls(before)[0][0] == "read_file":
            ((_, read),) = calls(before)
        opened = [given for name, given in calls(turn) if name == "create_issue"]
        if not opened:
            continue
        # One issue a turn. The turn before is the read of the file these
        # grants are in, or an issue of that file.
        assert [name for name, _ in calls(turn)] == ["create_issue"]
        assert [name for name, _ in calls(before)] in (["read_file"], ["create_issue"])
        assert read
        for given in opened:
            grant = given["title"].split(":")[0]
            assert f"\n{grant}: " in short_review.files[read["path"]]
            assert given["title"].startswith(f"{grant}: ")
            assert given["title"].endswith(code)
            assert len(given["title"]) <= 200
            titles.append(grant)

    assert sorted(titles) == sorted(key.key_full)
    assert len(titles) == 32


def test_the_scripted_reviewer_waits_inside_every_answer_when_asked(
    rules: ModuleType, short_review: Any
) -> None:
    parsed = parse_scenario(json.dumps(rules.scripted_reviewer(short_review, delay_seconds=2.0)))

    assert parsed.summary is not None
    assert parsed.summary.delay_seconds == 2.0
    assert {turn.delay_seconds for turn in parsed.turns} == {2.0}
    assert rules.DELAY == 2.0


def test_the_fake_reviewer_has_a_model_name_of_its_own(rules: ModuleType) -> None:
    assert rules.FAKE_MODEL == "fake-long-job"
    assert rules.FAKE_MODEL != rules.MODEL
