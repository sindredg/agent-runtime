"""The API application."""

from fastapi import FastAPI

app = FastAPI(title="agent-runtime")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness only: the process is up and serving. It does not check Postgres."""
    return {"status": "ok"}
