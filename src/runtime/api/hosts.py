"""Answer only requests addressed to the API by one of its names.

The API has no authentication and listens on localhost. A browser still
reaches localhost for any page it shows:

- A page can point a name it controls at 127.0.0.1 (DNS rebinding). Its
  requests then carry that name in `Host`, and the browser treats the API
  as the page's own site. Such a request is refused with 400.
- A page of another site, also one on another port of localhost, can send
  a form to the API. A change it sends carries that site in `Origin`, which
  is not the API's own, and is refused with 403. A read is left to the
  browser, which keeps the answer from the page: no CORS header is sent.
"""

import json
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

WRONG_HOST = "the request's Host is not one the API answers to"
FOREIGN_ORIGIN = "a change sent from another site's page is refused"
_READS = frozenset({"GET", "HEAD", "OPTIONS"})
# A name or IPv4 address and an optional port, nothing else.
_AUTHORITY = re.compile(r"([a-z0-9.-]+)(:[0-9]{1,5})?")


def _name(authority: str) -> str | None:
    """The host name of `host[:port]`, lowercased. None for anything else."""
    found = _AUTHORITY.fullmatch(authority.lower())
    return found.group(1) if found else None


def _values(scope: Scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope["headers"] if key == name]


class AllowedHosts:
    """ASGI middleware: check `Host` on every request, `Origin` on changes."""

    def __init__(self, app: ASGIApp, hosts: Iterable[str]) -> None:
        self.app = app
        self.hosts = frozenset(host.lower() for host in hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hosts = _values(scope, b"host")
        if len(hosts) != 1 or _name(hosts[0]) not in self.hosts:
            await _refuse(send, 400, WRONG_HOST)
            return
        origins = _values(scope, b"origin")
        # The API's own origin is the address the request was sent to. "null",
        # another port and anything that is not a URL are another site.
        if scope["method"] not in _READS and any(
            urlsplit(origin).netloc.lower() != hosts[0].lower() for origin in origins
        ):
            await _refuse(send, 403, FOREIGN_ORIGIN)
            return
        await self.app(scope, receive, send)


async def _refuse(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
