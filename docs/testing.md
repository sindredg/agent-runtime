# Testing

Six suites, each with its own needs. Nothing here calls a paid API except
the contract test, which is run by hand.

```mermaid
flowchart LR
    U[tests/unit] --> N[nothing]
    I[tests/integration] --> S[make up:<br/>Postgres, api, worker, fakes]
    E[tests/escape] --> IMG[the sandbox and guard images,<br/>no stack]
    C[tests/chaos] --> CS[make chaos:<br/>three workers, 2 s lease,<br/>failure switches]
    L[tests/lab] --> LS[make lab:<br/>Gitea, two workers,<br/>a failpoint]
    K[tests/contract] --> S
    K --> KEY[a Claude API key]
```

## Suites

| Suite | Run with | Covers |
|---|---|---|
| `tests/unit` | `make test` | Each module by itself: replay, the decisions, budget, scope rules, settings, the CLI, the fakes |
| `tests/integration` | `make up`, then `make integration` | The store on real Postgres, the worker against the fakes, the API over HTTP, the sandbox against Docker |
| `tests/escape` | `make escape` | Attempts to get out of the sandbox, and scoped calls against two targets |
| `tests/chaos` | `make chaos` | Workers that are killed, paused, crashed or stopped while jobs run |
| `tests/lab` | `make lab` | A worker stopped in a call to Gitea ([Lab demo](lab-demo.md)) |
| `tests/contract` | by hand | The fake provider against the Claude API |

- The integration and chaos suites never mock Postgres.
- Integration tests run store and worker code as `runtime_app`, as the
  stack does, so a missing right fails a test. The owner builds and
  migrates the test databases ([Database roles](database-roles.md)).
- They stop before the first test unless every worker is on the fake
  provider ([The Claude API](settings.md#the-claude-api)).
- `make chaos` and `make lab` replace the normal stack. Run `make up` afterwards.
- `CHAOS_RUNS` and `CHAOS_SEED` set the number and times of random kills.
- CI runs unit, integration, escape and the short chaos run on every pull
  request.

## Records a test trusts

A test that checks what happened outside a job does not read the job's own
step log. It reads:

- The fake provider's request log, and the fake tracker's or Gitea's list
  of issues.
- The `crash_worker` tool's log file, and a worker's container log.
- The records of a stand-in provider or tool.
- Docker's list of containers, and the Postgres log.
- A canary file on the host, in the escape suite.

## Must-fail runs

A check counts only if it can fail. Each tested guarantee has a run with its
mechanism switched off, which had to fail. The outcomes are in
[Results](results/README.md).

| Switch | Effect |
|---|---|
| `ESCAPE_DOCKER_DEFAULTS=1` | The escape attempts run as root with Docker's default container settings |
| `ESCAPE_NO_RULES=1` | The scope tests run with a guard that has no rules |

## Failure switches

These exist for `tests/chaos` and do nothing unless switched on.

| Switch | Effect |
|---|---|
| `FAILPOINT` | Names a point at which the worker ends its own process with exit status 137 |
| `FAILPOINT_TOOL` | Limits the three tool failpoints to calls of one tool |
| `CHAOS_TOOLS=1` | Offers the `crash_worker` tool, which logs a line and then ends the process |
| `delay_seconds` in a scenario turn | The fake provider waits that long inside the stream |

- The six failpoints: `after_claim`, `after_model_request`, `mid_stream`,
  `after_tool_intent`, `after_tool_ran`, `after_tool_result`.
- A failpoint fires only on a job with `takeovers` 0, so the worker that
  takes the job over gets past it.

`docker-compose.chaos.yml` sets up the chaos stack:

| Setting | Value |
|---|---|
| Workers | Three, restarted when they end themselves |
| Lease | 2 s, renewed every 0.5 s. The failpoint tests use 6 s and 2 s. |
| Poll | 0.2 s |
| Sandbox timeout | 10 s, so a takeover during a sandboxed call waits 40 s, not 60 s |
| `CHAOS_LOG_DIR` | `/chaos` in the worker, mounted from `./.chaos` on the host |

The tests that pause or stop a worker use the default 30 s lease. Each chaos
test writes its result to `.chaos/results.jsonl` with a `verdict`: `passed`,
`failed`, or `void` when the machine's clock was set during the run.

## The fakes

- The fake provider picks a scenario by the request's `model` and a turn by
  the number of assistant messages. It keeps the newest 10,000 requests.
- It counts tokens as a quarter of the characters of JSON, at least 1: input
  from `system`, `tools` and `messages`, output from the answer's content.
- It never answers with more output tokens than `max_tokens`. A longer turn
  is cut, with stop reason `max_tokens`.
- `PUT /_fake/busy` makes it answer 429 or 529 for a time, for one model or
  all ([Backoff](backoff.md)).
- The fake tracker stores up to 100,000 issues in memory and never merges
  two.

## The contract test

It compares the fake provider with the Claude API, both ways, and is run
by hand with a key: [The contract test](contract-test.md).

## Commands

| Command | What it does |
|---|---|
| `uv sync` | Create `.venv` from `uv.lock` |
| `uv run ruff check .` and `uv run ruff format --check .` | Lint and check formatting |
| `uv run mypy` | Type check (strict) |
| `uv run yamllint --strict .` | Lint YAML |
| `uv run python scripts/lint_json.py <files>` | Check JSON syntax |
| `uv run python scripts/lint_docs.py <files>` | Check the limits of the Markdown pages |
| `uv run pre-commit install` | Run the checks above before each commit. It does not run tests. |
| `uv run alembic upgrade head --sql` | Print the schema's SQL from `migrations/`. Needs `DATABASE_URL` set, not a database. |
