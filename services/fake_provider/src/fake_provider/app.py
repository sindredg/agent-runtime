"""The fake provider application. It serves no model responses yet."""

from fastapi import FastAPI

app = FastAPI(title="fake-provider")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
