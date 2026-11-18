# Testing

Five suites, each with its own needs. Nothing here calls a paid API except
the contract test, which is run by hand.

```mermaid
flowchart LR
    U[tests/unit] --> N[nothing]
    I[tests/integration] --> S[make up:<br/>Postgres, api, worker, fakes]
    E[tests/escape] --> IMG[the sandbox and guard images,<br/>no stack]
    C[tests/chaos] --> CS[make chaos:<br/>three workers, 2 s lease,<br/>failure switches]
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
| `tests/contract` | by hand | The fake provider against the Claude API |

- The integration and chaos suites never mock Postgres.
- Integration tests run store and worker code as `runtime_app`, as the
  stack does, so a missing right fails a test. The owner builds and
  migrates the test databases ([Database roles](database-roles.md)).
- They stop before the first test unless every worker is on the fake
  provider ([The Claude API](settings.md#the-claude-api)).
- `make chaos` replaces the normal stack. Run `make up` afterwards.
- `CHAOS_RUNS` sets the number of runs with a random kill time (default 20),
  `CHAOS_SEED` the times.
- CI runs unit, integration, escape and the short chaos run on every pull
  request.

## Records a test trusts

A test that checks what happened outside a job does not read the job's own
step log. It reads:

- The fake provider's request log and the fake tracker's list of issues.
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
- The fake tracker stores up to 100,000 issues in memory and never merges
  two.

## The contract test

```sh
make up
ANTHROPIC_API_KEY=<key> CONTRACT_MODEL=<model id> uv run pytest tests/contract
```

It sends five small requests to the Claude API, one of which is refused on
purpose. Everything the fake returns has to be in what the API returns.

It was run once on 2026-11-17, against `claude-haiku-5-5` at commit
`a27b0f8`, on one machine, with the fake provider of `make up` at the same
commit. 4 of 4 tests passed.

What the pass does not show:

- The comparison goes one way. What the API returns and the fake never does
  is not reported
  ([issue 72](https://github.com/sindredg/agent-runtime/issues/72)).
- The fake was not broken and then compared with the API. That the
  comparison reports a difference is shown on made-up responses, in
  `tests/unit/test_contract_comparison.py`.
- It is one exchange of two turns with one model.

An earlier version of the test ran on 2026-11-14 at commit `8f73f6e`. 3 of 4
passed: the fourth expected text before the tool call, and the model wrote
none.

Whole jobs have also run against the API:
[Runs against the Claude API](results/api.md).

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

The schema is defined by the Alembic migrations in `migrations/`. Print the
SQL without a database:

```sh
DATABASE_URL=postgresql://user:pass@host/db uv run alembic upgrade head --sql
```
