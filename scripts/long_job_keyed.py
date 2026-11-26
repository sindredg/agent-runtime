"""The long-job harness on the Claude API: what refuses a run, and the count of a whole corpus.

    python scripts/long_job_keyed.py check          why the run in this environment
                                                    may not start; exit 1 if it may not
    python scripts/long_job_keyed.py corpus-count   the tokens of a seed's whole corpus

`check` reads the run's LONG_JOB_* variables and prints the refusals of
`tests/long_job/rules.py`, one fixed sentence a line: the pass line is
proposed, the mode, the budget, the jobs or the token bound is not set, the
seed is missing, public or does not fit the commitment, or the seed's job
fails its checks. A run on the fake provider is refused nothing. It never
reads the key: the workflow runs it before the step that holds the key, and
`make long-job-claude` runs it before the stack starts.

`corpus-count` runs on the Claude API only. It refuses unless
LONG_JOB_PROVIDER is `claude`, the pass line is fixed, LONG_JOB_SEED is a
seed, and that seed is not the committed public one. Then it builds the
client with CLAUDE_API_KEY, counts the corpus of the seed's full-shape job in
index order as `tests/long_job/counting.py` counts a job, prints the count,
and exits 1 when it is partial or under the corpus floor.

Everything printed is a count or a fixed sentence. An error is printed as
its type's name, and a failed count with its HTTP status.
"""

import importlib.util
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SUITE = Path("tests/long_job")
NOT_CLAUDE = "corpus-count runs on the Claude API only: LONG_JOB_PROVIDER is not claude."
SDK_SETTINGS = ("ANTHROPIC_LOG", "ANTHROPIC_BASE_URL")


def suite(name: str, root: Path = ROOT) -> ModuleType:
    """A module of `tests/long_job/` of `root`, loaded by its path. It imports nothing of the
    stack, and no SDK."""
    module_name = f"long_job_keyed_{name}"
    spec = importlib.util.spec_from_file_location(module_name, root / SUITE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{SUITE / name}.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: the dataclasses in it look their module up.
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def check(env: Mapping[str, str], root: Path = ROOT) -> list[str]:
    """The refusals of the run in `env`, as fixed sentences. Empty when it may start."""
    found: list[str] = suite("rules", root).refusals(env, root)
    return found


def corpus_refusals(env: Mapping[str, str], root: Path, rules: ModuleType) -> list[str]:
    """Why a corpus count may not start: the provider, then refusals 3, 5 and 6 of the run.

    The mode, the budget and the commitment are not asked: the count comes
    before any of them is set.
    """
    if env.get(rules.PROVIDER, "") != rules.CLAUDE:
        return [NOT_CLAUDE]
    found = []
    if rules.LINE_FIXED is None:
        found.append(rules.PROPOSED)
    try:
        seed = rules.parse_seed(env.get(rules.SEED, ""))
    except rules.SeedError:
        return [*found, rules.NO_SEED]
    try:
        public = rules.read_seed(root / rules.PUBLIC_SEED_FILE)
    except rules.SeedError:
        public = None
    if public is None or seed == public:
        found.append(rules.PUBLIC_SEED)
    return found


def corpus_steps(files: Mapping[str, str]) -> list[dict[str, Any]]:
    """The corpus of a job, the index left out, as the tool results of reading it in order."""
    corpus = list(files.values())[1:]
    return [
        {"seq": seq, "kind": "tool_result", "payload": {"content": text, "is_error": False}}
        for seq, text in enumerate(corpus, start=1)
    ]


def corpus_count(env: Mapping[str, str], root: Path = ROOT) -> int:
    """Count a seed's whole corpus on the Claude API behind its refusals. The exit status."""
    rules, counting = suite("rules", root), suite("counting", root)
    refused = corpus_refusals(env, root, rules)
    if refused:
        print("\n".join(f"Not run: {sentence}" for sentence in refused), file=sys.stderr)
        return 1
    try:
        review = rules.generate(rules.parse_seed(env[rules.SEED]), rules.FULL_SHAPE)
    except Exception as error:
        # The type only: the text could quote the seed or a record.
        print(f"Not run: the seed's job was not made ({type(error).__name__}).", file=sys.stderr)
        return 1
    try:
        count = counting.crossing(
            corpus_steps(review.files),
            counting.counter(counting.keyed_client(env)),
            model=rules.MODEL,
            chunk_bytes=rules.CHUNK_BYTES,
        )
    except Exception as error:
        failed = counting.failure(error)
        print(f"The count failed: {failed['type']}, status {failed['status']}.", file=sys.stderr)
        return 1
    partial = ", partial" if count.partial else ""
    print(f"corpus count: {count.total} tokens in {count.chunks} requests{partial}")
    if count.partial or count.total < rules.CORPUS_FLOOR:
        print(f"Under the corpus floor of {rules.CORPUS_FLOOR}, or partial.", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    # Settings of the SDK a count would otherwise read: a request log, another address.
    for name in SDK_SETTINGS:
        os.environ.pop(name, None)
    match argv:
        case ["check"]:
            refused = check(os.environ)
            if refused:
                print("\n".join(f"Not run: {sentence}" for sentence in refused), file=sys.stderr)
                return 1
            print("Nothing refuses the run.")
            return 0
        case ["corpus-count"]:
            return corpus_count(os.environ)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
