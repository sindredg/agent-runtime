"""The issue trackers that `create_issue` can open issues in.

Each tracker can create an issue that carries the call's operation id, and
find that issue again by the id. The fake tracker stores the id as a field.
Gitea has no such field, so the id goes into the issue's last body line, and
the lookup lists issues instead of searching (ADR 0019).

The request that creates an issue goes through a client of its own, one that
can be given up: the tool puts one deadline on the whole request (ADR 0025).
"""

import socket
import struct
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2

from runtime.config import TrackerConfig

# Seconds for one request to the tracker.
TIMEOUT = 10.0

# What replaces the network in tests. It serves both kinds of client.
type Network = httpx2.MockTransport

# For the connection of a request that can be given up: closing it drops what
# the kernel has not sent yet, and resets the connection. Without it a
# request that was written but not delivered is sent again by the kernel
# after the close, for a minute or more, and can arrive long after it was
# given up. The option is the socket's, so a request that was answered ends
# with a reset too: a tracker may log one for each issue.
ABORTIVE_CLOSE = (socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def given_up_at_once(transport: Network | None) -> httpx2.AsyncBaseTransport:
    """The transport of a client whose request can be given up.

    A transport of its own does not go through a proxy named in the
    environment, which the client for reading does.
    """
    return transport or httpx2.AsyncHTTPTransport(socket_options=[ABORTIVE_CLOSE])


@dataclass(frozen=True, slots=True)
class Issue:
    number: int


class Refused(Exception):
    """The tracker answered a create request with an error status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"the tracker answered {status}")
        self.status = status


class Unclear(Exception):
    """The tracker's answer does not say whether the issue exists."""


class Tracker(Protocol):
    def client(self, transport: Network | None) -> httpx2.Client:
        """A client for reading from the tracker. `transport` replaces the network in tests."""
        ...

    def sender(self, transport: Network | None) -> httpx2.AsyncClient:
        """A client for the request that opens an issue. Giving the request up closes it."""
        ...

    async def create(
        self, http: httpx2.AsyncClient, title: str, body: str, operation_id: str
    ) -> Issue:
        """Open the issue. Raises `Refused`, `httpx2.HTTPError`, or on a malformed answer
        `ValueError`, `LookupError` or `TypeError`."""
        ...

    def find(self, http: httpx2.Client, operation_id: str) -> Issue | None:
        """The issue of this operation id, or None after a clear answer without one.
        Raises `Unclear` or `httpx2.HTTPError` otherwise."""
        ...


def _issue(entry: Any, number_key: str) -> Issue:
    """An issue from a tracker's JSON: its number. Raises if the entry is not one."""
    return Issue(int(entry[number_key]))


@dataclass(frozen=True, slots=True)
class FakeTracker:
    """The fake tracker of the compose stack. It stores the operation id with the issue."""

    url: str

    def client(self, transport: Network | None) -> httpx2.Client:
        return httpx2.Client(base_url=self.url, timeout=TIMEOUT, transport=transport)

    def sender(self, transport: Network | None) -> httpx2.AsyncClient:
        # No timeout per step: the tool's one deadline covers the whole request.
        return httpx2.AsyncClient(
            base_url=self.url, timeout=None, transport=given_up_at_once(transport)
        )

    async def create(
        self, http: httpx2.AsyncClient, title: str, body: str, operation_id: str
    ) -> Issue:
        response = await http.post(
            "/issues", json={"title": title, "body": body, "operation_id": operation_id}
        )
        if response.status_code != 201:
            raise Refused(response.status_code)
        return _issue(response.json(), "id")

    def find(self, http: httpx2.Client, operation_id: str) -> Issue | None:
        # This tracker shows an issue as soon as it has created it, which is
        # what makes an empty answer a clear no.
        response = http.get("/issues", params={"operation_id": operation_id})
        if response.status_code != 200:
            raise Unclear(f"the tracker answered {response.status_code}")
        try:
            found = response.json()
            if not isinstance(found, list):
                raise Unclear("the answer is not a list")
            return _issue(found[0], "id") if found else None
        except (ValueError, LookupError, TypeError) as error:
            raise Unclear(f"the answer is not a list of issues: {error!r}") from error


# The last line of every issue body the tool opens in Gitea.
MARKER = "agent-runtime operation-id: "
# Issues per page, Gitea's default maximum, and pages read before the answer
# is unknown.
PAGE = 50
MAX_PAGES = 20


def marked(body: str, operation_id: str) -> str:
    """The body as sent to Gitea: the operation id as its last line."""
    return f"{body}\n\n{MARKER}{operation_id}"


def marks(body: str, operation_id: str) -> bool:
    """True when the body ends with this operation id's marker line.

    The tool writes the marker after the model's text, so nothing the model
    wrote can end the body.
    """
    end, marker = body.rstrip(), f"{MARKER}{operation_id}"
    return end == marker or end.endswith(f"\n{marker}")


def _number(entry: Any) -> int:
    """The issue number of a list entry. Raises `Unclear` if it has no whole number."""
    number = entry.get("number") if isinstance(entry, dict) else None
    # bool is an int in Python, and JSON true is no issue number.
    if not isinstance(number, int) or isinstance(number, bool):
        raise Unclear("an entry has no issue number")
    return number


def _count(response: httpx2.Response) -> int:
    """Gitea's count of all issues the list holds, from `X-Total-Count`."""
    text = response.headers.get("X-Total-Count", "")
    if not (text.isascii() and text.isdigit()):
        raise Unclear("the answer has no issue count")
    return int(text)


@dataclass(frozen=True, slots=True)
class GiteaTracker:
    """A repository in Gitea. The token needs `write:issue` and `read:user`."""

    url: str
    # `owner/name`.
    repo: str
    token: str = field(repr=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}", "Accept": "application/json"}

    def client(self, transport: Network | None) -> httpx2.Client:
        return httpx2.Client(
            base_url=f"{self.url}/api/v1",
            headers=self._headers(),
            timeout=TIMEOUT,
            transport=transport,
        )

    def sender(self, transport: Network | None) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            base_url=f"{self.url}/api/v1",
            headers=self._headers(),
            timeout=None,
            transport=given_up_at_once(transport),
        )

    async def create(
        self, http: httpx2.AsyncClient, title: str, body: str, operation_id: str
    ) -> Issue:
        response = await http.post(
            f"/repos/{self.repo}/issues",
            json={"title": title, "body": marked(body, operation_id)},
        )
        if response.status_code != 201:
            raise Refused(response.status_code)
        return _issue(response.json(), "number")

    def find(self, http: httpx2.Client, operation_id: str) -> Issue | None:
        """Read every page of the token's user's issues. Not found only after the whole list.

        Gitea's search goes through a full-text index, which can lag behind
        and does not match an operation id exactly. A plain list reads the
        database. The user comes from the token, so the list holds the
        issues this tool opened, whatever the settings say.
        """
        user = self._user(http)
        # Issue numbers, not entries: an issue listed on two pages would
        # otherwise stand in for one that was never listed (issue 126).
        seen: set[int] = set()
        first_total = None
        for page in range(1, MAX_PAGES + 1):
            entries, total = self._page(http, user, page)
            # A deletion while the list is read moves issues up past the
            # page being read. It also lowers the count, which is how it shows.
            if first_total is not None and total != first_total:
                raise Unclear(f"the count changed from {first_total} to {total} during the list")
            first_total = total
            for entry in entries:
                ours = self._is_ours(entry, user, operation_id)
                number = _number(entry)
                if ours:
                    return _issue(entry, "number")
                if number in seen:
                    raise Unclear(f"issue {number} is listed twice")
                seen.add(number)
            if len(entries) < PAGE:
                if len(seen) != total:
                    raise Unclear(f"saw {len(seen)} of {total} issues")
                return None
        raise Unclear(f"more than {MAX_PAGES * PAGE} issues by {user}")

    def _user(self, http: httpx2.Client) -> str:
        """The name of the user the token belongs to."""
        response = http.get("/user")
        if response.status_code != 200:
            raise Unclear(f"the tracker answered {response.status_code} for the token's user")
        login = response.json()["login"]
        if not isinstance(login, str) or not login:
            raise Unclear("the token's user has no name")
        return login

    def _page(self, http: httpx2.Client, user: str, page: int) -> tuple[list[Any], int]:
        response = http.get(
            f"/repos/{self.repo}/issues",
            params={
                "state": "all",
                "type": "issues",
                "created_by": user,
                "limit": PAGE,
                "page": page,
            },
        )
        if response.status_code != 200:
            raise Unclear(f"the tracker answered {response.status_code}")
        entries = response.json()
        if not isinstance(entries, list):
            raise Unclear("the answer is not a list")
        return entries, _count(response)

    def _is_ours(self, entry: Any, user: str, operation_id: str) -> bool:
        try:
            body, author = entry["body"], entry["user"]["login"]
        except (LookupError, TypeError) as error:
            raise Unclear(f"an entry is not an issue: {error!r}") from error
        if not isinstance(body, str) or not isinstance(author, str):
            raise Unclear("an issue body or author is not a string")
        # Gitea's user names ignore case. Another author in a list asked for
        # one user's issues is an answer that cannot be trusted.
        if author.lower() != user.lower():
            raise Unclear("the list holds an issue by another user")
        return marks(body, operation_id)


def tracker_for(config: TrackerConfig) -> Tracker:
    """The tracker the worker's settings name."""
    if config.kind == "gitea":
        return GiteaTracker(config.url, config.repo, config.token)
    return FakeTracker(config.url)
