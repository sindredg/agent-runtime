"""A job's target scope: what its tools may reach on the network (ADR 0009).

The scope is part of the job spec. Each target is an address, an address
range or a host name, with optional ports. The worker resolves names once per
call, refuses anything in the deny list, and turns the rest into the firewall
rules of the call's guard container. The model has no part in any of it.
"""

import asyncio
import ipaddress
import re
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

MAX_TARGETS = 64
MAX_PORTS = 64
# Seconds to resolve one name before the call is refused.
RESOLVE_TIMEOUT = 5.0
# A host name: letters, digits and hyphens in dot-separated labels. No
# wildcard: a name is resolved when the call starts.
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_NAME = re.compile(rf"(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*")

# Never reachable, whatever the scope says.
FIXED_DENY: tuple[Network, ...] = (
    # Docker's own DNS server in every container on a user-defined network.
    # A tool gets no resolver (ADR 0009), and DNS would let it send data out.
    ipaddress.ip_network("127.0.0.11/32"),
    # Link-local, with the cloud metadata address 169.254.169.254.
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)


def denied(network: Network, deny: Iterable[Network]) -> Network | None:
    """The deny-list entry that `network` touches, or None."""
    for entry in deny:
        if network.version == entry.version and network.overlaps(entry):
            return entry
    return None


class ScopeRefused(Exception):
    """A call's scope cannot be used: a name did not resolve, or led into the deny list."""


def _network(host: str) -> Network | None:
    """The address or range `host` names, or None for a host name."""
    try:
        return ipaddress.ip_network(host, strict=True)
    except ValueError as error:
        if "/" in host:
            # A range with host bits set, or a malformed one: not a name either.
            raise ValueError(f"{host!r} is not an address range: {error}") from error
        return None


class Target(BaseModel):
    """One thing a job's tools may reach: a host, and if given, only these ports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # An address (10.0.0.5), a range (10.0.0.0/24) or a host name (lab.example).
    host: str = Field(min_length=1, max_length=253)
    # TCP and UDP ports. Left out: every port.
    ports: tuple[int, ...] | None = Field(default=None, min_length=1, max_length=MAX_PORTS)

    @field_validator("host")
    @classmethod
    def _host_is_an_address_a_range_or_a_name(cls, host: str) -> str:
        network = _network(host)
        if network is None:
            name = host.lower()
            if not _NAME.fullmatch(name):
                raise ValueError(f"{host!r} is not an address, a range or a host name")
            return name
        if network.is_loopback or network.is_unspecified:
            raise ValueError(f"{host!r} is a loopback or unspecified address")
        entry = denied(network, FIXED_DENY)
        if entry is not None:
            # Known now. The rest of the deny list, the runtime's own
            # networks, only the worker knows: it refuses those per call.
            raise ValueError(f"{host!r} is in the deny list ({entry}), which no scope can open")
        return str(network) if network.num_addresses > 1 else str(network.network_address)

    @field_validator("ports")
    @classmethod
    def _ports_are_ports(cls, ports: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if ports is None:
            return None
        if any(isinstance(port, bool) or not 1 <= port <= 65535 for port in ports):
            raise ValueError("a port must be a number from 1 to 65535")
        if len(set(ports)) != len(ports):
            raise ValueError("a port is given twice")
        return tuple(sorted(ports))


@dataclass(frozen=True, slots=True)
class Allowed:
    """One target as the rules see it: addresses, and the ports, None for all."""

    host: str
    networks: tuple[Network, ...]
    ports: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class Resolved:
    """A scope ready for one call: what is allowed, and the names to pin."""

    allowed: tuple[Allowed, ...]
    # name -> addresses, written into the guard's hosts file.
    hosts: tuple[tuple[str, tuple[str, ...]], ...]

    def record(self) -> list[dict[str, object]]:
        """For the call's `tool_intent`: each target with the addresses it was given."""
        return [
            {
                "host": allowed.host,
                "addresses": [str(network) for network in allowed.networks],
                "ports": list(allowed.ports) if allowed.ports is not None else None,
            }
            for allowed in self.allowed
        ]


async def _addresses(name: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(RESOLVE_TIMEOUT):
            found = await loop.getaddrinfo(name, None, type=socket.SOCK_STREAM)
    except (OSError, TimeoutError) as error:
        raise ScopeRefused(f"{name!r} could not be resolved: {error}") from error
    addresses = sorted({str(info[4][0]) for info in found})
    if not addresses:
        raise ScopeRefused(f"{name!r} resolved to no address")
    return tuple(addresses)


async def resolve(targets: Sequence[Target], deny: Sequence[Network]) -> Resolved:
    """Resolve the names of a scope and check every address against the deny list.

    Raises ScopeRefused for a name that does not resolve, and for any target
    that touches the deny list: a range that covers part of it is refused
    whole, so no scope can open a hole in it.
    """
    allowed, hosts = [], []
    for target in targets:
        network = _network(target.host)
        if network is not None:
            networks: tuple[Network, ...] = (network,)
        else:
            addresses = await _addresses(target.host)
            networks = tuple(ipaddress.ip_network(address) for address in addresses)
            hosts.append((target.host, addresses))
        for candidate in networks:
            entry = denied(candidate, deny)
            if entry is not None:
                raise ScopeRefused(
                    f"{target.host!r} ({candidate}) is in the deny list ({entry}),"
                    " which no scope can open"
                )
        allowed.append(Allowed(target.host, networks, target.ports))
    return Resolved(tuple(allowed), tuple(hosts))


def _addresses_rule(networks: Sequence[Network]) -> list[str]:
    """`ip daddr { ... }` and `ip6 daddr { ... }` matches, one per family present."""
    rules = []
    for family, keyword in ((4, "ip"), (6, "ip6")):
        members = [str(network) for network in networks if network.version == family]
        if members:
            rules.append(f"{keyword} daddr {{ {', '.join(members)} }}")
    return rules


def ruleset(scope: Resolved, deny: Sequence[Network]) -> str:
    """The nftables rules of a call's guard. Built from addresses and ports only.

    Output: the deny list first, then the namespace's own loopback, replies,
    the scope's targets, and a counted drop of everything else. Input: only
    loopback and replies.
    """
    output = [f"{match} counter name dropped drop" for match in _addresses_rule(deny)]
    output += ['oifname "lo" accept', "ct state established,related accept"]
    for allowed in scope.allowed:
        for match in _addresses_rule(allowed.networks):
            if allowed.ports is None:
                output.append(f"{match} accept")
            else:
                ports = ", ".join(str(port) for port in allowed.ports)
                output.append(f"{match} tcp dport {{ {ports} }} accept")
                output.append(f"{match} udp dport {{ {ports} }} accept")
    output.append("counter name dropped drop")
    lines = [
        "table inet guard {",
        "  counter dropped {",
        "  }",
        "  chain output {",
        "    type filter hook output priority 0; policy drop;",
        *(f"    {rule}" for rule in output),
        "  }",
        "  chain input {",
        "    type filter hook input priority 0; policy drop;",
        '    iifname "lo" accept',
        "    ct state established,related accept",
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def parse_dropped(text: str) -> int | None:
    """The packet count from `nft list counter inet guard dropped`, or None."""
    match = re.search(r"\bpackets (\d+)\b", text)
    return int(match[1]) if match else None
