"""A spec whose files fill the whole limit, from submit to a worker that takes it over.

Real Postgres and the stack's fake provider. The API and both workers run
in the test process.
"""

import asyncio
import dataclasses
import hashlib
import threading
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine

from runtime.provider.messages import AnthropicProvider
from runtime.store.jobs import claim_job
from runtime.store.status import JobStatus
from runtime.tools.base import ToolCall, ToolResult
from runtime.worker.agent import run_job

from .conftest import Stack, expire_lease, stub_tool
from .test_api import Api, api_test

# The documented limit, written out: a smaller constant fails the test.
LIMIT = 8 * 1024 * 1024
CHUNK = 16 * 1024

type Digests = dict[str, str]


def full_files() -> dict[str, str]:
    """Files whose paths and contents add up to exactly LIMIT bytes.

    Many files of 16 KiB, then one that fills what is left. Each content
    differs, so a file written under the wrong name shows in its digest.
    """
    files = {"hello.txt": "Hi\n"}
    used = len("hello.txt") + len("Hi\n")
    index = 0
    while True:
        path = f"seed/{index:04d}.txt"
        if LIMIT - used - len(path) < CHUNK:
            break
        pattern = hashlib.sha256(path.encode()).hexdigest()
        files[path] = (pattern * (CHUNK // len(pattern) + 1))[:CHUNK]
        used += len(path) + CHUNK
        index += 1
    path = "seed/rest.txt"
    files[path] = "r" * (LIMIT - used - len(path))
    return files


def size(files: dict[str, str]) -> int:
    return sum(len(path.encode()) + len(content.encode()) for path, content in files.items())


def digests_of(workspace: Path) -> Digests:
    return {
        file.relative_to(workspace).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in workspace.rglob("*")
        if file.is_file()
    }


def test_files_at_the_limit_survive_submit_run_takeover_and_read(
    stack: Stack, tmp_path: Path
) -> None:
    files = full_files()
    assert size(files) == LIMIT
    expected = {
        path: hashlib.sha256(content.encode()).hexdigest() for path, content in files.items()
    }
    spec = {"prompt": "Read hello.txt and say what it holds.", "model": "hello", "files": files}

    # What each run of the tool saw in its workspace.
    seen: list[Digests] = []
    hanging, release = threading.Event(), threading.Event()

    def look(call: ToolCall) -> ToolResult:
        seen.append(digests_of(call.workspace))
        if len(seen) == 1:
            hanging.set()
            release.wait(timeout=60)
        return ToolResult("Hi")

    tools = stub_tool(look)
    # As on another host: the second worker seeds the workspace from the stored spec.
    first_settings = stack.settings()
    second_settings = dataclasses.replace(first_settings, workspace_root=tmp_path / "other-host")

    async def body(api: Api) -> tuple[httpx2.Response, JobStatus | None, dict[str, Any]]:
        submitted = await api.client.post("/jobs", json=spec)
        assert submitted.status_code == 201, f"a spec at the limit was refused: {submitted.text}"
        provider = AnthropicProvider(api_key="test", base_url=stack.fake_provider_url)
        try:
            job_id = UUID(submitted.json()["id"])
            second_status = await abandon_then_take_over(api.engine, provider, job_id)
        finally:
            await provider.aclose()
        read = await api.client.get(f"/jobs/{job_id}")
        over = {**files, "seed/rest.txt": files["seed/rest.txt"] + "r"}
        refused = await api.client.post("/jobs", json={**spec, "files": over})
        return refused, second_status, read.json()

    async def abandon_then_take_over(
        engine: AsyncEngine, provider: AnthropicProvider, job_id: UUID
    ) -> JobStatus | None:
        async with engine.begin() as conn:
            first_claim = await claim_job(conn, worker_id="w1")
        assert first_claim is not None
        assert first_claim.id == job_id
        first = asyncio.create_task(run_job(engine, provider, tools, first_claim, first_settings))
        assert await asyncio.to_thread(hanging.wait, 30), "the first worker never ran the tool"
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        async with engine.begin() as conn:
            await expire_lease(conn, first_claim.id)
            second_claim = await claim_job(conn, worker_id="w2")
        assert second_claim is not None
        assert (second_claim.id, second_claim.takeovers) == (job_id, 1)
        return await run_job(engine, provider, tools, second_claim, second_settings)

    refused, second_status, read = api_test(stack.database_url, body)

    assert second_status is JobStatus.SUCCEEDED
    assert len(seen) == 2
    assert seen[0] == expected, "the first worker's workspace differs from the spec"
    assert seen[1] == expected, "the workspace after the takeover differs from the spec"
    assert read["status"] == "succeeded"
    assert read["spec"] == spec
    # One byte more is refused, and the message names the limit.
    assert refused.status_code == 422
    [error] = refused.json()["detail"]
    assert error["msg"].endswith(f"files hold {LIMIT + 1} bytes; the limit is {LIMIT}")
