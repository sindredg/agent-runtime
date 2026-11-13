# agent-runtime

A platform that runs long AI agent jobs reliably. A job is submitted, an agent
works on it for minutes or hours (call the model, run a tool, record the
result, repeat), and the runtime keeps the job alive through worker crashes,
provider rate limits, and bad tool output.

## Status

Planned. The repository holds the project scaffold, the CI pipeline, and a
Docker Compose stack of placeholder services that start and report healthy. No
runtime behaviour is built yet.

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

The stack runs five services from `docker-compose.yml`:

| Service | Role | Today |
|---|---|---|
| `postgres` | Jobs and the step log | Postgres 18, empty database |
| `migrate` | Prepares the database, then exits | Checks the connection only |
| `api` | HTTP API on `127.0.0.1:8000` | `GET /healthz` only |
| `worker` | Runs jobs | Idles |
| `fake-provider` | Scripted stand-in for the model provider | `GET /healthz` only |

Copy `.env.example` to `.env` to change the database password or the API port.

Individual checks:

| Command | What it does |
|---|---|
| `uv sync` | Create `.venv` from `uv.lock` |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Check formatting |
| `uv run mypy` | Type check (strict) |
| `uv run pytest tests/unit` | Unit tests |
| `uv run yamllint --strict .` | Lint YAML |
| `uv run pre-commit install` | Run the same checks before each commit |
