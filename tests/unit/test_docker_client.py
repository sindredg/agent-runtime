"""The Docker client: which calls it makes, and what it does with Docker's answers."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx2
import pytest

from runtime.sandbox.docker import Docker, DockerError, SandboxError

type Handler = Callable[[httpx2.Request], httpx2.Response]


def with_docker[T](handler: Handler, body: Callable[[Docker], Awaitable[T]]) -> T:
    async def go() -> T:
        docker = Docker("/unused", transport=httpx2.MockTransport(handler))
        try:
            return await body(docker)
        finally:
            await docker.aclose()

    return asyncio.run(go())


def test_create_sends_the_config_to_the_pinned_api_version_and_returns_the_id() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(201, json={"Id": "abc", "Warnings": []})

    assert with_docker(handler, lambda docker: docker.create({"Image": "x"})) == "abc"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1.45/containers/create"
    assert json.loads(seen[0].content) == {"Image": "x"}


def test_an_error_status_carries_dockers_message() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"message": "No such image: x"})

    with pytest.raises(DockerError, match="404: No such image: x") as raised:
        with_docker(handler, lambda docker: docker.create({"Image": "x"}))
    assert raised.value.status == 404


def test_a_daemon_that_cannot_be_reached_is_a_sandbox_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no socket", request=request)

    with pytest.raises(SandboxError, match="could not be reached"):
        with_docker(handler, lambda docker: docker.start("abc"))


def test_remove_forces_and_accepts_a_container_that_is_gone() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(404, json={"message": "No such container"})

    with_docker(handler, lambda docker: docker.remove("abc"))
    assert seen[0].method == "DELETE"
    assert seen[0].url.path == "/v1.45/containers/abc"
    assert seen[0].url.params["force"] == "1"


def test_wait_returns_the_exit_status() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"StatusCode": 3})

    assert with_docker(handler, lambda docker: docker.wait("abc")) == 3


async def frames() -> AsyncIterator[bytes]:
    # A stream in two chunks, as a socket may deliver it.
    yield b"\x01\x00\x00\x00"
    yield b"\x00\x00\x00\x01x"


def test_attach_yields_the_raw_stream() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1.45/containers/abc/attach"
        assert dict(request.url.params) == {"stream": "1", "stdout": "1", "stderr": "1"}
        return httpx2.Response(200, content=frames())

    async def body(docker: Docker) -> bytes:
        async with docker.attach("abc") as stream:
            return b"".join([chunk async for chunk in stream])

    assert with_docker(handler, body) == b"\x01\x00\x00\x00\x00\x00\x00\x01x"


def test_attach_refused_is_a_docker_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(409, json={"message": "container is stopped"})

    async def body(docker: Docker) -> None:
        async with docker.attach("abc"):
            pass

    with pytest.raises(DockerError, match="409: container is stopped"):
        with_docker(handler, body)
