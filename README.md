# agent-runtime

A platform that runs long AI agent jobs. A job is submitted, an agent works on
it (call the model, run a tool, record the result, repeat), and every step is
written to a log in Postgres. When a worker goes away in the middle of a job,
another worker continues it from that log.

## Status

Built and tested: a job survives its worker being killed, paused or stopped.
A worker holds a job through a lease and renews it while the job runs. When
the lease runs out, another worker takes the job over and resumes from the
step log. Writes of a worker that lost its job are refused. A tool call that
was interrupted is run again only when that is known to be safe; otherwise
the job waits for a person to approve. A job that takes its worker down every
time is failed after five takeovers. See [Results](#results).

Not built: retry on provider errors and rate limits, token budgets, deadlines,
cancel, and a sandbox for tools. Tools run in the worker process. The full
list is in [Known limits](docs/architecture.md#known-limits).

Locally the model provider is a fake that answers from scenario files, so
nothing here calls a paid API.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Docker with Compose, and make.

```sh
git clone https://github.com/sindredg/agent-runtime.git
cd agent-runtime
make up
uv run runtime submit examples/hello.json --follow
```

`make up` builds the image and starts Postgres, the API, one worker, the fake
provider and a fake issue tracker. The job in `examples/hello.json` asks the
agent to read one file. The output is the job id, one line per step, and the
outcome:

```text
01a1d655-88bd-7468-afc4-5ca3a1947906
   1  model_request   model=hello messages=1
   2  model_response  stop_reason=tool_use tokens=221+44 I will read hello.txt from the workspace.
   3  tool_intent     read_file {"path": "hello.txt"}
   4  tool_result     ok Hello from the workspace.
   5  model_request   model=hello messages=3
   6  model_response  stop_reason=end_turn tokens=314+22 The file hello.txt holds a short greeting. The job is done.
succeeded  tokens_used=601
```

The text of the two responses is scripted in the fake provider's `hello`
scenario. The tool call and its result are real: the worker read the file.

A second example changes something outside the runtime. It reads a failed CI
log and opens an issue in the fake tracker:

```sh
uv run runtime submit examples/ci-triage.json --follow
curl http://127.0.0.1:8002/issues
```

```sh
uv run runtime status <job id>
uv run runtime logs <job id>
uv run runtime approve <job id>
make clean
```

`runtime approve` is for a job in `needs_approval`: a worker went away during
a tool call and nobody can tell whether the call happened. Approving runs the
call again, so look at the system the tool changes first. A command that
follows such a job prints how to approve it and exits with status 3. `make
clean` stops the stack and deletes its database.

## Job files

`runtime submit` posts a JSON file to the API.

| Field | Required | Meaning |
|---|---|---|
| `prompt` | yes | The first user message. Not empty. |
| `system` | no | System prompt. Not empty if given. |
| `model` | no | Model id. Default: the worker's `MODEL`. With the fake provider it names a scenario. |
| `files` | no | Text files written into the job's workspace before it starts: relative path to content. 1 MiB in total, paths included. Paths are plain relative paths: no `..`, no `./`, no trailing `/`, and no path that is also a directory of another. |
| `token_budget` | no | At least 1. Stored with the job. Not enforced yet. |
| `timeout_seconds` | no | 1 to 604800 (one week). Sets the job's deadline. Not enforced yet. |

Any other field is refused.

The agent has two tools. `read_file` reads a text file of up to 64 KiB from
the job's workspace. `create_issue` opens an issue in the tracker at
`TRACKER_URL` and is offered only when that is set.

## Results

Each guarantee has a test with a pass line that was fixed before the run, and
a run with the mechanism switched off to show that the test can fail. What
happened outside a job is read from records other than the job's step log:
the fake provider's request log, the fake tracker's list of issues, the crash
tool's own log file, and for the paused worker its container log.

| Guarantee | Status | Test and result | With the mechanism off |
|---|---|---|---|
| A job survives a worker stopping | Tested | 200 runs. In each, a job was submitted and the worker holding it at a random time from a seed was killed with `docker kill`: 171 kills were sent. 200 of 200 succeeded, each with the same final conversation as a job nobody interrupted, and the provider was never asked an earlier turn again. 116 of the jobs were taken over in the middle. The other 84 were not interrupted: at 29 kill times no worker held the job (before the claim or after the end), and 55 kills landed after the job had finished. One more run per failpoint, with the worker ending its own process: 6 of 6. | Resume switched off (the log is not replayed): the provider is asked turns 0, 1, 0, 1, 2 and the test fails. |
| A tool call that is unsafe to repeat does not run twice without approval, with [one accepted exception](docs/architecture.md#known-limits) for a worker paused at the wrong instant | Tested | The same 206 runs: no operation id has more than one issue in the tracker, and no approval was needed. Worker stopped after the tool ran: the check finds the issue and the result is recovered. Worker stopped after the intent: the check finds nothing and the call runs once more, one issue. One run with the tracker frozen so the check gets no answer: the job waits in `needs_approval`, and after the approval the tracker holds two issues for one approval. | Intent write skipped: two issues for one operation id. Every tool treated as idempotent: two issues for one operation id. |
| A stale worker cannot corrupt a job | Tested | A worker waiting for a model answer was paused with `docker pause` for 45 s under the default 30 s lease. Another worker took the job over after 29.3 s. The first was woken while the second was in the middle of the job. It wrote no step after the takeover, sent no further request to the provider and opened no issue. The job succeeded with one takeover. | Epoch check removed from the fence: the woken worker marks the job `failed` under the second worker. |
| A job that crashes its worker every time does not loop forever | Tested | A job whose tool ends the worker's process: `failed` after 5 takeovers, with 6 runs in the tool's own log. | Limit removed: the job was still running after 180 s and had been taken over 34 times. |
| Provider rate limits slow jobs, they do not fail them | Not built | | |
| Throughput scales with workers | Not measured | | |
| Every job stays inside its token budget and deadline, and can be cancelled | Not built | | |
| A tool cannot leave its sandbox | Not built | | |
| Bad tool output does not break a job | Partly built, not proven | A tool call that fails becomes an error result and the job goes on. The output cap and the tool timeout are not built. | |

Also tested, outside the table: a worker stopped with `docker stop` (SIGTERM)
handed its job back in 1.2 s and the job finished on another worker with no
takeover counted.

| | |
|---|---|
| Command | `CHAOS_RUNS=200 make chaos` |
| Commit | `256fe08` for the 200 runs, on 2026-11-13. Review fixes came after it. The short run (20 runs with 15 kills sent, and every other test: 11 of 11 tests) passed on them at `665bf6a` on 2026-11-14. |
| Runs and seed | 200 runs with a random kill time, seed `20261113`. Every other test: 1 run. |
| Workers | 3 |
| Provider | The fake provider. Scenario `ci-triage-slow` (each answer 0.5 s late) for the kills and failpoints, `ci-triage-stall` (two answers 10 s late) for the pause and the stop, `crash-loop` for the crash loop. |
| Lease | 2 s, renewed every 0.5 s, for the kills, the failpoints and the crash loop. The default 30 s, renewed every 10 s, for the pause and the stop. |
| Where | Docker Compose on one machine, Linux aarch64 under WSL2, 8 CPUs |

What these results do not show:

- Nothing ran against a real model provider or a real issue tracker.
- Everything ran on one host with one clock. A lost network between a worker
  and the database is covered by an integration test that cuts the database
  sessions, not by a chaos run.
- The runs with a mechanism switched off were read when they ran. Their
  output was not kept.
- Runs that do not count, and why:
  - One earlier run of the pause test had two takeovers instead of one. The
    machine's wall clock moved forward by 36.6 s during the test: a container
    log that was written ten times a second has a gap of that length, and the
    test's own monotonic clock saw no pause. That ended the second worker's
    lease early. The paused worker still wrote nothing and the job succeeded
    with one issue. A lease ends by the database's clock, so a clock that is
    set forward is a [known limit](docs/architecture.md#known-limits). The
    pause test passed in the run above and in six runs before it.
  - One short run at `665bf6a` was aborted: the machine was suspended for
    about eight hours in the middle of it. The run was repeated and passed.
  - The first trial of the kill test, before any result was recorded, failed
    6 of 6: the test compared each job with a reference job of a different
    scenario, whose tool call ids differ. The test was corrected.
  - The chaos tests now compare the wall clock with the monotonic clock and
    fail a test during which they drift apart by more than a second.
- The limits that the design accepts are listed under
  [Known limits](docs/architecture.md#known-limits). The main one: fencing
  protects the database, not the outside world. A worker that is paused at
  the wrong instant can still run one tool once after it wakes.

## Design

- [Architecture](docs/architecture.md): components, data model, what happens
  when a worker goes away, every mechanism, settings, known limits.
- [ADR 0001](docs/adr/0001-build-durability-on-postgres.md): build job
  durability on Postgres instead of adopting a workflow engine.
- [ADR 0002](docs/adr/0002-postgres-as-the-job-queue.md): use Postgres as the
  job queue.
- [ADR 0003](docs/adr/0003-official-sdk-and-a-wire-compatible-fake.md): call
  the model through the official SDK, and test against a fake provider.
- [ADR 0004](docs/adr/0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md):
  fence a worker's writes with the lease epoch and a row lock, and accept one
  gap.
- [ADR 0005](docs/adr/0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md):
  rerun an interrupted tool call only when that is known to be safe, otherwise
  ask a person.
- [ADR 0006](docs/adr/0006-count-takeovers-apart-from-claims.md): count
  takeovers apart from claims, and release the job when a worker is told to
  stop.

## Development

uv installs the pinned Python and tools into `.venv`.

| Command | What it does |
|---|---|
| `make up` | Build the image, start every service, wait until all are healthy |
| `make logs` | Follow the service logs |
| `make down` | Stop the services, keep the database volume |
| `make clean` | Stop the services, delete the database volume |
| `make test` | Unit tests |
| `make integration` | Integration tests. Needs the stack from `make up`. |
| `make chaos` | Chaos tests. Starts the stack with three workers and a 2 second lease, then kills, pauses and crashes workers. `CHAOS_RUNS` sets the number of runs with a random kill time (default 20), `CHAOS_SEED` the times. Run `make up` afterwards to get the normal stack back. |

The stack publishes four ports, all on `127.0.0.1` only: the API on 8000,
Postgres on 5432, the fake provider on 8001 and the fake tracker on 8002.
Copy `.env.example` to `.env` to change a port, the database password, the
lease length, or the provider the worker calls.

The API has no authentication. Do not publish its port on another interface.

Individual checks:

| Command | What it does |
|---|---|
| `uv sync` | Create `.venv` from `uv.lock` |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Check formatting |
| `uv run mypy` | Type check (strict) |
| `uv run pytest` | Unit tests |
| `uv run pytest tests/integration` | Integration tests (needs `make up`) |
| `uv run pytest -s tests/chaos` | Chaos tests (needs the stack that `make chaos` starts) |
| `uv run yamllint --strict .` | Lint YAML |
| `uv run python scripts/lint_json.py <files>` | Check JSON syntax |
| `uv run pre-commit install` | Run lint, format, type, JSON and YAML checks before each commit. It does not run tests. |

The contract test compares the fake provider with the Claude API. It sends
five small requests to the Claude API, one of which is refused on purpose, and
is run by hand:

```sh
make up
ANTHROPIC_API_KEY=<key> CONTRACT_MODEL=<model id> uv run pytest tests/contract
```

It was run once, on 2026-11-14, against `claude-haiku-5-5` at commit 8f73f6e.
Three of four tests passed: the runtime's client completed a tool call and
its answer against the API, and both providers refuse an unknown model with
the same error shape. The fourth test failed on an assumption of its own. It
expected text before the tool call in the same response, and the model wrote
none. The comparison now covers both responses of the exchange. That version
has not been run against the API yet.

The schema is defined by the Alembic migrations in `migrations/`. Print the
SQL without a database:

```sh
DATABASE_URL=postgresql://user:pass@host/db uv run alembic upgrade head --sql
```
