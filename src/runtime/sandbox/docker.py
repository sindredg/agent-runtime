"""The few calls of the Docker Engine API the sandbox needs, over the unix socket.

The API is plain HTTP, so the worker's HTTP client is enough and no Docker
library is added (ADR 0008).
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import httpx2

# The oldest API version with `Subpath` on a volume mount, which gives a
# container one job's directory of the workspace volume. A newer daemon
# answers older versions down to its minimum (1.44 for Docker 29).
API_VERSION = "1.45"
# Seconds for one request. Reading the output of a running command has no
# such limit: the command's own timeout bounds it.
REQUEST_TIMEOUT = 10.0


class SandboxError(Exception):
    """Docker did not do what the sandbox asked, or could not be reached."""


class DockerError(SandboxError):
    """Docker answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Docker answered {status}: {message}")
        self.status = status


def _message(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    message = body.get("message") if isinstance(body, dict) else None
    return message[:500] if isinstance(message, str) else response.text[:500]


@contextmanager
def _unreachable() -> Iterator[None]:
    """A transport failure becomes a SandboxError."""
    try:
        yield
    except httpx2.HTTPError as error:
        raise SandboxError(f"Docker could not be reached: {error!r}") from error


class Docker:
    """A client for one Docker daemon. Close it with `aclose`."""

    def __init__(self, socket_path: str, *, transport: httpx2.AsyncBaseTransport | None = None):
        self._http = httpx2.AsyncClient(
            transport=transport or httpx2.AsyncHTTPTransport(uds=socket_path),
            # The host name is not used on a unix socket. The path carries
            # the API version, so every call speaks the same one.
            base_url=f"http://docker/v{API_VERSION}",
            timeout=REQUEST_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, *ok: int, **kwargs: Any) -> httpx2.Response:
        with _unreachable():
            response = await self._http.request(method, path, **kwargs)
        if response.status_code not in ok:
            raise DockerError(response.status_code, _message(response))
        return response

    async def create(self, name: str, config: dict[str, Any]) -> None:
        """Create a container with this name. The image must be there: nothing is pulled.

        The other calls take the name in place of an id.
        """
        await self._send("POST", "/containers/create", 201, params={"name": name}, json=config)

    @asynccontextmanager
    async def attach(self, container_id: str) -> AsyncIterator[AsyncIterator[bytes]]:
        """The container's stdout and stderr as raw frames, from before it starts to its end.

        Attach before `start`, or the first output can be missed.
        """
        with _unreachable():
            async with self._http.stream(
                "POST",
                f"/containers/{container_id}/attach",
                params={"stream": "1", "stdout": "1", "stderr": "1"},
                timeout=httpx2.Timeout(REQUEST_TIMEOUT, read=None),
            ) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise DockerError(response.status_code, _message(response))
                yield response.aiter_raw()

    async def start(self, container_id: str) -> None:
        await self._send("POST", f"/containers/{container_id}/start", 204)

    async def wait(self, container_id: str) -> int:
        """Wait until the container has stopped and return its exit status."""
        response = await self._send("POST", f"/containers/{container_id}/wait", 200)
        try:
            status = response.json()["StatusCode"]
        except ValueError, LookupError, TypeError:
            status = None
        if not isinstance(status, int):
            raise SandboxError("Docker gave no exit status")
        return status

    async def remove(self, container_id: str) -> None:
        """Remove the container, killing it first if it still runs. Gone already is fine."""
        await self._send("DELETE", f"/containers/{container_id}", 204, 404, params={"force": "1"})
