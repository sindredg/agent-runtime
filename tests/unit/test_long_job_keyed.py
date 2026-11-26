"""`scripts/long_job_keyed.py`: what refuses a keyed long-job run, and the count of a corpus.

No request leaves the process. The SDK's client class is replaced by a
recorder: a refused count builds none, and an open one builds it once,
with the key, and counts with a stub. The seed is one of the tests' own.
"""

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar

import httpx2
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "long_job_keyed.py"
# A seed of the tests' own, and the committed public one written two other ways.
OTHER_SEED = "00112233445566778899aabbccddeeff"
PUBLIC = json.loads((ROOT / "tests" / "unit" / "access_review_seed.json").read_text())["seed"]
NOT_A_KEY = "sk-ant-not-a-key-of-the-tests"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("long_job_keyed", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["long_job_keyed"] = module
    spec.loader.exec_module(module)
    return module


keyed = load()


class Recorder:
    """Stands in for `anthropic.Anthropic`: keeps how it was built, and counts bytes plus 7."""

    built: ClassVar[list[dict[str, Any]]] = []
    fails: ClassVar[Exception | None] = None

    def __init__(self, **given: Any) -> None:
        Recorder.built.append(given)
        self.messages = self

    def count_tokens(self, *, model: str, messages: list[dict[str, Any]]) -> Any:
        if Recorder.fails is not None:
            raise Recorder.fails
        return SimpleNamespace(input_tokens=len(messages[0]["content"].encode()) + 7)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> type[Recorder]:
    import anthropic

    Recorder.built, Recorder.fails = [], None
    monkeypatch.setattr(anthropic, "Anthropic", Recorder)
    return Recorder


class Modules(dict[str, ModuleType]):
    """The suite's modules as the script loads them, kept, and a way to change one of them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__({name: keyed.suite(name) for name in ("rules", "counting")})
        self.monkeypatch = monkeypatch

    def change(self, name: str, **values: Any) -> None:
        for attribute, value in values.items():
            self.monkeypatch.setattr(self[name], attribute, value)


@pytest.fixture
def modules(monkeypatch: pytest.MonkeyPatch) -> Modules:
    loaded = Modules(monkeypatch)
    monkeypatch.setattr(keyed, "suite", lambda name, root=ROOT: loaded[name])
    return loaded


def env(**more: str) -> dict[str, str]:
    return {"LONG_JOB_PROVIDER": "claude", "CLAUDE_API_KEY": NOT_A_KEY, **more}


# check


def test_check_refuses_nothing_on_the_fake_provider() -> None:
    for provider in ({}, {"LONG_JOB_PROVIDER": ""}, {"LONG_JOB_PROVIDER": "fake"}):
        assert keyed.check({**provider, "LONG_JOB_SEED": "not a seed"}) == []


def test_check_refuses_every_keyed_run_today_and_quotes_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in env(LONG_JOB_MODE="must-fail", LONG_JOB_SEED=OTHER_SEED).items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LONG_JOB_TOKEN_BUDGET", raising=False)
    code = keyed.main(["check"])
    printed = capsys.readouterr()

    assert code == 1
    lines = printed.err.splitlines()
    assert "Not run: The pass line is proposed." in lines
    assert "Not run: The token bound of keyed long jobs is not set, or the run is over it." in lines
    assert all(line.startswith("Not run: ") for line in lines)
    assert OTHER_SEED not in printed.out + printed.err
    assert NOT_A_KEY not in printed.out + printed.err


def test_check_refuses_a_seed_whose_job_fails_its_checks(modules: Modules) -> None:
    """With a seed whose generated job is made to fail a check of the generator."""
    rules = modules["rules"]
    given = env(LONG_JOB_MODE="must-fail", LONG_JOB_SEED=OTHER_SEED)

    assert rules.CHECKS not in keyed.check(given)
    modules.change("rules", problems=lambda review: ["a check of the tests' own"])
    assert rules.CHECKS in keyed.check(given)


def test_the_check_command_passes_when_nothing_refuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(keyed, "check", lambda env: [])
    assert keyed.main(["check"]) == 0
    assert capsys.readouterr().out == "Nothing refuses the run.\n"
    assert keyed.main(["nothing"]) == 2


# corpus-count: each refusal alone, with every other gate open


def opened(modules: Modules) -> None:
    modules.change("rules", LINE_FIXED="2026-11-26")


@pytest.mark.parametrize(
    ("given", "line_fixed", "sentence"),
    [
        # A key and no provider: no client.
        ({"CLAUDE_API_KEY": NOT_A_KEY, "LONG_JOB_SEED": OTHER_SEED}, True, "NOT_CLAUDE"),
        (env(LONG_JOB_PROVIDER="fake", LONG_JOB_SEED=OTHER_SEED), True, "NOT_CLAUDE"),
        # The pass line is proposed.
        (env(LONG_JOB_SEED=OTHER_SEED), False, "PROPOSED"),
        # No seed, or not a seed.
        (env(), True, "NO_SEED"),
        (env(LONG_JOB_SEED="not a seed"), True, "NO_SEED"),
        # The committed public seed, also in capitals and padded.
        (env(LONG_JOB_SEED=PUBLIC), True, "PUBLIC_SEED"),
        (env(LONG_JOB_SEED=f"  {PUBLIC.upper()}\n"), True, "PUBLIC_SEED"),
    ],
    ids=["key-no-provider", "fake", "proposed", "no-seed", "not-a-seed", "public", "public-padded"],
)
def test_a_refused_corpus_count_builds_no_client(
    modules: Modules,
    recorder: type[Recorder],
    capsys: pytest.CaptureFixture[str],
    given: dict[str, str],
    line_fixed: bool,
    sentence: str,
) -> None:
    rules = modules["rules"]
    if line_fixed:
        opened(modules)
    wanted = keyed.NOT_CLAUDE if sentence == "NOT_CLAUDE" else getattr(rules, sentence)

    assert keyed.corpus_refusals(given, ROOT, rules) == [wanted]
    assert keyed.corpus_count(given) == 1
    printed = capsys.readouterr()
    assert printed.err == f"Not run: {wanted}\n"
    assert recorder.built == []
    assert NOT_A_KEY not in printed.out + printed.err


def test_an_open_corpus_count_builds_the_client_once_and_prints_the_count_only(
    modules: Modules,
    recorder: type[Recorder],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from access_review import FULL, generate
    from runtime.provider.messages import CLAUDE_API_URL

    opened(modules)
    code = keyed.corpus_count(env(LONG_JOB_SEED=OTHER_SEED))
    printed = capsys.readouterr()
    review = generate(bytes.fromhex(OTHER_SEED), FULL)
    corpus = list(review.files.values())[1:]

    assert code == 0
    assert recorder.built == [{"api_key": NOT_A_KEY, "base_url": CLAUDE_API_URL}]
    (line,) = printed.out.splitlines()
    assert line.startswith("corpus count: ") and line.endswith(" requests")
    # The stub's count: each request's bytes plus 7, less the one-character baseline.
    total = int(line.split()[2])
    chunks = int(line.split()[5])
    assert total == sum(len(text.encode()) for text in corpus) - chunks
    assert printed.err == ""
    assert corpus[0].splitlines()[0] not in printed.out
    assert NOT_A_KEY not in printed.out


def test_the_corpus_is_every_file_but_the_index_in_its_order() -> None:
    files = {"access/0001.txt": "one", "index.txt": "the list", "access/0002.txt": "two"}
    steps = keyed.corpus_steps(files)
    assert [step["payload"]["content"] for step in steps] == ["one", "two"]
    assert [step["seq"] for step in steps] == [1, 2]
    with pytest.raises(ValueError, match=r"no index\.txt"):
        keyed.corpus_steps({"access/0001.txt": "one"})


def test_a_corpus_under_the_floor_or_a_failed_count_exits_1_with_counts_only(
    modules: Modules,
    recorder: type[Recorder],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import anthropic

    opened(modules)
    modules.change("rules", CORPUS_FLOOR=10**9)
    assert keyed.corpus_count(env(LONG_JOB_SEED=OTHER_SEED)) == 1
    assert "Under the corpus floor" in capsys.readouterr().err

    words = f"refused for {NOT_A_KEY}"
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/count_tokens")
    recorder.fails = anthropic.RateLimitError(
        words, response=httpx2.Response(429, request=request), body=None
    )
    assert keyed.corpus_count(env(LONG_JOB_SEED=OTHER_SEED)) == 1
    printed = capsys.readouterr()
    assert printed.err == "The count failed: RateLimitError, status 429.\n"
    assert words not in printed.out + printed.err


def test_a_seed_whose_job_cannot_be_made_is_named_by_the_errors_type_only(
    modules: Modules,
    recorder: type[Recorder],
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened(modules)

    def broken(seed: bytes, shape: Any) -> Any:
        raise ValueError(f"cannot make the job of {OTHER_SEED}")

    modules.change("rules", generate=broken)
    assert keyed.corpus_count(env(LONG_JOB_SEED=OTHER_SEED)) == 1
    printed = capsys.readouterr()
    assert printed.err == "Not run: the seed's job was not made (ValueError).\n"
    assert recorder.built == []
