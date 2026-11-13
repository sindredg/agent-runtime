# agent-runtime

A platform that runs long AI agent jobs. A job is submitted, an agent works on
it (call the model, run a tool, record the result, repeat), and every step is
written to a log in Postgres.

The goal is that a job survives worker crashes, provider rate limits and bad
tool output. That part is planned, not built: see [Results](#results).

## Status

Built: one job runs end to end. The API accepts it, a worker claims it and
runs the agent loop against a model provider, each step lands in the step
log, and the CLI follows it live. Locally the provider is a fake that answers
from scenario files, so nothing here calls a paid API.

Not built: everything that makes a job durable. A job whose worker stops stays
`running`. Budgets, deadlines and cancel are not enforced. Tools run in the
worker process without a sandbox. The full list is in
[Known limits](docs/architecture.md#known-limits).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Docker with Compose, and make.

```sh
git clone https://github.com/sindredg/agent-runtime.git
cd agent-runtime
make up
uv run runtime submit examples/hello.json --follow
```

`make up` builds the image and starts Postgres, the API, one worker and the
fake provider. The job in `examples/hello.json` asks the agent to read one
file. The output is the job id, one line per step, and the outcome:

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

```sh
uv run runtime status <job id>
uv run runtime logs <job id>
make clean
```

`make clean` stops the stack and deletes its database.

## Job files

`runtime submit` posts a JSON file to the API.

| Field | Required | Meaning |
|---|---|---|
| `prompt` | yes | The first user message |
| `system` | no | System prompt |
| `model` | no | Model id. Default: the worker's `MODEL`. With the fake provider it names a scenario. |
| `files` | no | Text files written into the job's workspace before it starts: relative path to content. 1 MiB in total. |
| `token_budget` | no | Stored with the job. Not enforced yet. |
| `timeout_seconds` | no | Sets the job's deadline. Not enforced yet. |

The agent has one tool, `read_file`, which reads a text file of up to 64 KiB
from the job's workspace.

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
| Bad tool output does not break a job | Not built |

## Design

- [Architecture](docs/architecture.md): components, data model, what each
  mechanism does today and what is planned, settings, known limits.
- [ADR 0001](docs/adr/0001-build-durability-on-postgres.md): build job
  durability on Postgres instead of adopting a workflow engine.
- [ADR 0002](docs/adr/0002-postgres-as-the-job-queue.md): use Postgres as the
  job queue.
- [ADR 0003](docs/adr/0003-official-sdk-and-a-wire-compatible-fake.md): call
  the model through the official SDK, and test against a fake provider.

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

The stack publishes three ports, all on `127.0.0.1` only: the API on 8000,
Postgres on 5432 and the fake provider on 8001. Copy `.env.example` to `.env`
to change a port, the database password, or the provider the worker calls.

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
| `uv run yamllint --strict .` | Lint YAML |
| `uv run pre-commit install` | Run the same checks before each commit |

The contract test compares the fake provider with the Claude API. It makes
four small paid requests and is run by hand:

```sh
make up
ANTHROPIC_API_KEY=<key> CONTRACT_MODEL=<model id> uv run pytest tests/contract
```

The schema is defined by the Alembic migrations in `migrations/`. Print the
SQL without a database:

```sh
DATABASE_URL=postgresql://user:pass@host/db uv run alembic upgrade head --sql
```
