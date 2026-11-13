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
   2  model_response  stop_reason=tool_use tokens=123+44 I will read hello.txt from the workspace.
   3  tool_intent     read_file {"path": "hello.txt"}
   4  tool_result     ok Hello from the workspace.
   5  model_request   model=hello messages=3
   6  model_response  stop_reason=end_turn tokens=216+22 The file hello.txt holds a short greeting. The job is done.
succeeded  tokens_used=405
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
call again, so look at the system the tool changes first. `make clean` stops
the stack and deletes its database.

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

Each guarantee gets a test with a pass line that is fixed before the run, and
a run with the mechanism switched off to show the test can fail. A row is
filled in when that test has run. None has.

| Guarantee | Result |
|---|---|
| A job survives a worker stopping | Not built |
| A tool call that is unsafe to repeat never runs twice without approval | Not built |
| A stale worker cannot corrupt a job | Not built |
| A job that crashes its worker every time does not loop forever | Not built |
| Provider rate limits slow jobs, they do not fail them | Not built |
| Throughput scales with workers | Not measured |
| Every job stays inside its token budget and deadline, and can be cancelled | Not built |
| A tool cannot leave its sandbox | Not built |
| Bad tool output does not break a job | Partly built, not proven. A tool call that fails becomes an error result and the job goes on. The output cap and the tool timeout are not built. |

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
| `make chaos` | Chaos tests. Starts the stack with three workers and a 2 second lease, then kills, pauses and crashes workers. `CHAOS_RUNS` sets the number of random kills (default 20), `CHAOS_SEED` their times. Run `make up` afterwards to get the normal stack back. |

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
four small requests to the Claude API, one of which is refused on purpose, and
is run by hand. It has not been run yet:

```sh
make up
ANTHROPIC_API_KEY=<key> CONTRACT_MODEL=<model id> uv run pytest tests/contract
```

The schema is defined by the Alembic migrations in `migrations/`. Print the
SQL without a database:

```sh
DATABASE_URL=postgresql://user:pass@host/db uv run alembic upgrade head --sql
```
