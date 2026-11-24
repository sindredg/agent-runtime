"""G8 with a scope: a command reaches its job's targets and nothing else (ADR 0009).

Two targets listen on the guards' network: one in the scope, one not. Each
test runs `run_shell` calls of a job with a scope and checks what was reached,
what was dropped and counted, and that the rules cannot be changed from
inside. ESCAPE_NO_RULES=1 runs the same tests with a guard that sets no
rules, and ESCAPE_DOCKER_DEFAULTS=1 with the command put on the network
directly. Both must fail.
"""

import asyncio
import ipaddress
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from runtime.logs import text_safe
from runtime.scope import Allowed, Resolved

from .conftest import GUARD_IMAGE, IMAGE, Host, docker_cli

PORT = 8080
# Seconds a connection may take before it counts as blocked.
CONNECT = 3
LISTEN = (
    "perl -MIO::Socket::INET -e '$| = 1; $s = IO::Socket::INET->new(LocalPort => {port},"
    ' Listen => 5, ReuseAddr => 1) or die "listen: $!"; print "listening\\n";'
    ' while ($c = $s->accept) {{ print $c "hello from {name}\\n"; close $c }}\''
)


@dataclass
class Targets:
    inside: str
    outside: str


def address(container: str, network: str) -> str:
    return docker_cli(
        "inspect",
        "-f",
        f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
        container,
    ).strip()


@pytest.fixture
def targets(host: Host) -> Iterator[Targets]:
    network = host.settings.network
    assert network is not None
    names = {}
    for role in ("inside", "outside"):
        name = f"escape-target-{role}-{uuid.uuid4().hex[:8]}"
        docker_cli(
            "run", "--detach", "--name", name, "--network", network, IMAGE,
            "sh", "-c", LISTEN.format(port=PORT, name=role),
        )  # fmt: skip
        names[role] = name
    deadline = time.monotonic() + 30
    while not all("listening" in docker_cli("logs", name) for name in names.values()):
        assert time.monotonic() < deadline, "the targets did not start listening"
        time.sleep(0.1)
    yield Targets(address(names["inside"], network), address(names["outside"], network))
    for name in names.values():
        docker_cli("rm", "--force", name)


def only(
    *hosts: str,
    ports: tuple[int, ...] | None = None,
    pinned: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> Resolved:
    """A resolved scope, as the worker builds it from the job's spec."""
    allowed = tuple(Allowed(host, (ipaddress.ip_network(host),), ports) for host in hosts)
    return Resolved(allowed, pinned)


def connect(host: str, port: int = PORT) -> str:
    """Prints `connected`, `refused` (reached, port closed) or `blocked` (no answer)."""
    return (
        f"timeout {CONNECT} bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>/dev/null;"
        " case $? in 0) echo connected ;; 124) echo blocked ;; *) echo refused ;; esac"
    )


def greet(host: str) -> str:
    """Prints the target's greeting, or nothing."""
    return f"timeout {CONNECT} bash -c 'exec 3<>/dev/tcp/{host}/{PORT} && head -1 <&3'"


def test_the_target_in_scope_is_reached_and_the_one_outside_is_dropped(
    host: Host, targets: Targets
) -> None:
    result = host.shell(
        f"{greet(targets.inside)}; {connect(targets.outside)}", only(targets.inside)
    )
    print(text_safe(f"\n{result.content!r} dropped={result.dropped_packets}"))

    assert result.content.splitlines()[:2] == ["hello from inside", "blocked"]
    # Counted by the guard, and told to the model.
    assert result.dropped_packets is not None and result.dropped_packets > 0
    assert "packets to addresses outside the job's scope were dropped" in result.content


def test_only_the_scopes_ports_of_a_target_are_open(host: Host, targets: Targets) -> None:
    # Nothing listens on 9090: allowed, the target would refuse at once.
    result = host.shell(
        f"{greet(targets.inside)}; {connect(targets.inside, 9090)}",
        only(targets.inside, ports=(PORT,)),
    )
    print(text_safe(f"\n{result.content!r}"))

    assert result.content.splitlines()[:2] == ["hello from inside", "blocked"]


def test_a_name_in_the_scope_resolves_as_pinned_and_no_other_name_resolves(
    host: Host, targets: Targets
) -> None:
    scope = only(targets.inside, pinned=(("lab.test", (targets.inside,)),))

    result = host.shell(f"{greet('lab.test')}; getent hosts example.com || echo no-lookup", scope)
    print(text_safe(f"\n{result.content!r} dropped={result.dropped_packets}"))

    assert result.content.splitlines()[:2] == ["hello from inside", "no-lookup"]


@pytest.mark.parametrize(
    ("allowed", "script"),
    [
        ("169.254.169.254", connect("169.254.169.254", 80)),
        # Docker forwards TCP 53 here to its resolver: allowed, it connects.
        ("127.0.0.11", connect("127.0.0.11", 53)),
        ("127.0.0.11", "getent hosts example.com > /dev/null && echo connected || echo blocked"),
    ],
)
def test_the_deny_list_holds_even_when_a_scope_allows_it(
    host: Host, allowed: str, script: str
) -> None:
    """Built by hand past the check at resolution: the rules still drop it, one address per call."""
    scope = Resolved((Allowed(allowed, (ipaddress.ip_network(allowed),), None),), ())

    result = host.shell(script, scope)
    print(text_safe(f"\n{result.content!r} dropped={result.dropped_packets}"))

    assert result.content.splitlines()[0] == "blocked"
    assert result.dropped_packets is not None and result.dropped_packets > 0


def test_the_rules_cannot_be_changed_from_inside(host: Host, targets: Targets) -> None:
    """A command with nft at hand, as root in the namespace, without capabilities."""

    async def attack() -> str:
        call = asyncio.ensure_future(host.shell_async("sleep 30", only(targets.inside)))
        try:
            deadline = time.monotonic() + 30
            while not (guards := docker_cli(
                "ps", "--quiet", "--filter", f"label=agent-runtime.job={host.job}",
                "--filter", f"ancestor={GUARD_IMAGE}",
            ).split()):  # fmt: skip
                assert time.monotonic() < deadline, "no guard started"
                await asyncio.sleep(0.2)
            await asyncio.sleep(1)
            return await asyncio.to_thread(
                docker_cli,
                "run", "--rm", "--network", f"container:{guards[0]}", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "0:0", GUARD_IMAGE,
                "bash", "-c",
                f"nft flush ruleset 2>&1; echo flush=$?; {connect(targets.outside)}",
            )  # fmt: skip
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)

    said = asyncio.run(attack())
    print(text_safe(f"\n{said!r}"))

    assert "Operation not permitted" in said
    assert "flush=0" not in said
    assert said.splitlines()[-1] == "blocked"
