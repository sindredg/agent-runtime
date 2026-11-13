# agent-runtime

A platform that runs long AI agent jobs reliably. A job is submitted, an agent
works on it for minutes or hours (call the model, run a tool, record the
result, repeat), and the runtime keeps the job alive through worker crashes,
provider rate limits, and bad tool output.

## Status

Planned. The repository holds the project scaffold, the CI pipeline, the
database schema, and a Docker Compose stack of placeholder services that start
and report healthy. No runtime behaviour is built yet.

## Design decisions

| Record | Decision |
|---|---|
| [ADR 0001](docs/adr/0001-build-durability-on-postgres.md) | Build job durability on Postgres instead of adopting a workflow engine |
| [ADR 0002](docs/adr/0002-postgres-as-the-job-queue.md) | Use Postgres as the job queue |

## Development

Requires [uv](https://docs.astral.sh/uv/), Docker with Compose, and make. uv
installs the pinned Python and tools into `.venv`.

| Command | What it does |
|---|---|
| `make up` | Build the image, start every service, wait until all are healthy |
| `make logs` | Follow the service logs |
| `make down` | Stop the services, keep the database volume |
| `make clean` | Stop the services, delete the database volume |
| `make test` | Unit tests |
| `make integration` | Integration tests against the running stack's Postgres |

The stack runs five services from `docker-compose.yml`:

| Service | Role | Today |
|---|---|---|
| `postgres` | Jobs and the step log | Postgres 18 on `127.0.0.1:5432`, `jobs` and `steps` tables |
| `migrate` | Applies the schema migrations, then exits | `alembic upgrade head` |
| `api` | HTTP API on `127.0.0.1:8000` | `GET /healthz` only |
| `worker` | Runs jobs | Idles |
| `fake-provider` | Scripted stand-in for the model provider | `GET /healthz` only |

Copy `.env.example` to `.env` to change the database password or the published
ports. The integration tests connect to `DATABASE_URL`, which defaults to the
development database on `127.0.0.1:5432`.

The schema is defined by the Alembic migrations in `migrations/`. Print the
SQL without a database:

```sh
DATABASE_URL=postgresql://user:pass@host/db uv run alembic upgrade head --sql
```

Individual checks:

| Command | What it does |
|---|---|
| `uv sync` | Create `.venv` from `uv.lock` |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Check formatting |
| `uv run mypy` | Type check (strict) |
| `uv run pytest tests/unit` | Unit tests |
| `uv run pytest tests/integration` | Integration tests (needs `make up`) |
| `uv run yamllint --strict .` | Lint YAML |
| `uv run pre-commit install` | Run the same checks before each commit |
