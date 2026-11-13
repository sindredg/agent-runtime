# agent-runtime

A platform that runs long AI agent jobs reliably. A job is submitted, an agent
works on it for minutes or hours (call the model, run a tool, record the
result, repeat), and the runtime keeps the job alive through worker crashes,
provider rate limits, and bad tool output.

## Status

Planned. The repository holds the project scaffold and the CI pipeline. No
runtime behaviour is built yet.

## Development

Requires [uv](https://docs.astral.sh/uv/). uv installs the pinned Python and
tools into `.venv`.

| Command | What it does |
|---|---|
| `uv sync` | Create `.venv` from `uv.lock` |
| `uv run ruff check .` | Lint |
| `uv run ruff format --check .` | Check formatting |
| `uv run mypy` | Type check (strict) |
| `uv run pytest tests/unit` | Unit tests |
| `uv run yamllint --strict .` | Lint YAML |
| `uv run pre-commit install` | Run the same checks before each commit |
