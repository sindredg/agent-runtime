"""A job's scope: what is accepted at submit, what resolution refuses, and the rules it gives."""

import asyncio
import ipaddress
import socket
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from runtime import scope as scope_module
from runtime.sandbox.runner import LABEL, SandboxSettings, container_config, guard_config
from runtime.scope import (
    FIXED_DENY,
    Resolved,
    ScopeRefused,
    Target,
    parse_dropped,
    resolve,
    ruleset,
)
from runtime.spec import JobSpec

DENY = (*FIXED_DENY, ipaddress.ip_network("172.21.0.0/16"))
JOB = uuid.UUID("01a1d655-88bd-7468-afc4-5ca3a1947906")
SETTINGS = SandboxSettings(
    image="sandbox:test", volume="workspaces", guard_image="guard:test", network="sandbox-net"
)


def scope(*targets: dict[str, Any]) -> tuple[Target, ...]:
    spec = JobSpec.model_validate({"prompt": "go", "scope": list(targets)})
    assert spec.scope is not None
    return spec.scope


# What the API accepts


def test_addresses_ranges_and_names_are_accepted_in_their_plain_form() -> None:
    parsed = scope(
        {"host": "10.0.0.5", "ports": [443, 80]},
        {"host": "10.0.1.0/24"},
        {"host": "Lab.Example"},
        {"host": "2001:db8::1"},
    )

    assert [(t.host, t.ports) for t in parsed] == [
        ("10.0.0.5", (80, 443)),
        ("10.0.1.0/24", None),
        ("lab.example", None),
        ("2001:db8::1", None),
    ]


@pytest.mark.parametrize(
    "target",
    [
        {"host": "10.0.0.5/24"},
        {"host": "*.example"},
        {"host": "lab example"},
        {"host": "-lab.example"},
        {"host": "127.0.0.1"},
        {"host": "0.0.0.0"},  # noqa: S104 - a target, not a bind address
        {"host": "::1"},
        {"host": "10.0.0.5", "ports": [0]},
        {"host": "10.0.0.5", "ports": [65536]},
        {"host": "10.0.0.5", "ports": [80, 80]},
        {"host": "10.0.0.5", "ports": []},
        {"host": "10.0.0.5", "protocol": "tcp"},
        {"host": ""},
    ],
)
def test_a_target_that_cannot_be_read_is_refused(target: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        scope(target)


def test_an_empty_scope_or_one_too_long_is_refused() -> None:
    with pytest.raises(ValidationError):
        JobSpec.model_validate({"prompt": "go", "scope": []})
    with pytest.raises(ValidationError):
        JobSpec.model_validate(
            {"prompt": "go", "scope": [{"host": f"10.0.0.{n}"} for n in range(1, 66)]}
        )


def test_a_job_without_a_scope_has_none() -> None:
    assert JobSpec.model_validate({"prompt": "go"}).scope is None


# What resolution refuses


@pytest.mark.parametrize("host", ["169.254.169.254", "169.254.0.0/16", "0.0.0.0/0", "fe80::1"])
def test_a_target_in_the_fixed_deny_list_is_refused_at_submit(host: str) -> None:
    with pytest.raises(ValidationError, match="deny list"):
        scope({"host": host})


@pytest.mark.parametrize("host", ["172.21.3.4", "172.0.0.0/8"])
def test_a_target_that_touches_the_workers_deny_list_is_refused_whole(host: str) -> None:
    """The runtime's own networks are known to the worker only, so it refuses them per call."""
    with pytest.raises(ScopeRefused, match="deny list"):
        asyncio.run(resolve(scope({"host": host}), DENY))


def test_a_name_that_resolves_into_the_deny_list_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def metadata(name: str) -> tuple[str, ...]:
        return ("203.0.113.7", "169.254.169.254")

    monkeypatch.setattr(scope_module, "_addresses", metadata)

    with pytest.raises(ScopeRefused, match=r"169\.254\.169\.254"):
        asyncio.run(resolve(scope({"host": "lab.example"}), DENY))


def test_a_name_is_pinned_to_what_it_resolved_to(monkeypatch: pytest.MonkeyPatch) -> None:
    async def two(name: str) -> tuple[str, ...]:
        return ("203.0.113.7", "203.0.113.8")

    monkeypatch.setattr(scope_module, "_addresses", two)

    resolved = asyncio.run(resolve(scope({"host": "lab.example", "ports": [22]}), DENY))

    assert resolved.hosts == (("lab.example", ("203.0.113.7", "203.0.113.8")),)
    assert resolved.record() == [
        {"host": "lab.example", "addresses": ["203.0.113.7/32", "203.0.113.8/32"], "ports": [22]}
    ]


def test_a_name_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unknown(*args: object, **kwargs: object) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", unknown)

    with pytest.raises(ScopeRefused, match="could not be resolved"):
        asyncio.run(resolve(scope({"host": "no-such-host.invalid"}), DENY))


# The rules


def rules_of(*targets: dict[str, Any]) -> list[str]:
    resolved = asyncio.run(resolve(scope(*targets), DENY))
    return [line.strip() for line in ruleset(resolved, DENY).splitlines()]


def test_the_deny_list_comes_before_anything_is_allowed() -> None:
    rules = rules_of({"host": "203.0.113.0/24", "ports": [80, 443]})
    output = rules[rules.index("chain output {") + 2 : rules.index("chain input {") - 1]

    assert output == [
        "ip daddr { 127.0.0.11/32, 169.254.0.0/16, 172.21.0.0/16 } counter name dropped drop",
        "ip6 daddr { fe80::/10 } counter name dropped drop",
        'oifname "lo" accept',
        "ct state established,related accept",
        "ip daddr { 203.0.113.0/24 } tcp dport { 80, 443 } accept",
        "ip daddr { 203.0.113.0/24 } udp dport { 80, 443 } accept",
        "counter name dropped drop",
    ]
    assert "type filter hook output priority 0; policy drop;" in rules
    assert "type filter hook input priority 0; policy drop;" in rules


def test_a_target_without_ports_allows_every_port_and_ipv6_gets_ip6() -> None:
    rules = rules_of({"host": "2001:db8::/64"}, {"host": "203.0.113.9"})

    assert "ip6 daddr { 2001:db8::/64 } accept" in rules
    assert "ip daddr { 203.0.113.9/32 } accept" in rules


def test_the_count_of_dropped_packets_is_read_from_the_guard() -> None:
    said = "table inet guard {\n\tcounter dropped {\n\t\tpackets 12 bytes 720\n\t}\n}\n"

    assert parse_dropped(said) == 12
    assert parse_dropped("nft: no such table") is None


# The guard and the tool container


def test_the_guard_has_net_admin_only_and_pins_the_names() -> None:
    resolved = Resolved(allowed=(), hosts=(("lab.example", ("203.0.113.7",)),))

    config = guard_config(SETTINGS, JOB, 3, resolved)
    host = config["HostConfig"]

    assert config["Image"] == "guard:test"
    assert host["NetworkMode"] == "sandbox-net"
    assert host["CapDrop"] == ["ALL"]
    assert host["CapAdd"] == ["NET_ADMIN"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["ExtraHosts"] == ["lab.example:203.0.113.7"]
    assert config["Labels"][LABEL] == "1"
    assert any(entry.startswith("RULES=table inet guard {") for entry in config["Env"])


def test_a_scoped_command_joins_its_guard_and_nothing_else_changes() -> None:
    plain = container_config(SETTINGS, JOB, 3, ["ls"])
    scoped = container_config(SETTINGS, JOB, 3, ["ls"], guard="guard-1")

    assert scoped["HostConfig"]["NetworkMode"] == "container:guard-1"
    assert scoped["NetworkDisabled"] is False
    assert plain["HostConfig"]["NetworkMode"] == "none"
    assert plain["NetworkDisabled"] is True
    for config in (plain, scoped):
        config["HostConfig"].pop("NetworkMode")
        config.pop("NetworkDisabled")
    assert plain == scoped


@pytest.mark.parametrize(
    "changes",
    [
        {"guard_image": "guard:test", "network": None},
        {"guard_image": None, "network": "sandbox-net"},
        {"deny": (ipaddress.ip_network("10.0.0.0/8"),)},
    ],
)
def test_guard_settings_that_do_not_fit_together_are_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"guard_image|deny"):
        SandboxSettings(image="sandbox:test", volume="workspaces", **changes)
