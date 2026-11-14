# 0009: Enforce a job's target scope in the network namespace of its tools

Status: accepted, 2026-11-14

## Context

A security agent's tools need the network: a scan or a request has to reach
its target. They must reach nothing else. A scope written in the prompt does
not hold. The model can ignore it, and a page fetched from a target can tell
the agent to go elsewhere.

APTS asks for a scope check before every network action (SE-006), a deny
list that nothing overrides (SE-009), and a boundary the agent cannot change
(SC-019).

## Options

| Option | For | Against |
|---|---|---|
| No network for any tool | Simplest. Nothing to get wrong. | The jobs this runtime is for cannot run. |
| An HTTP proxy with an allowlist of hosts | Sees names and URLs, so it can allow one site on a shared address. | Web traffic only. A port scan or any other protocol either bypasses it or is impossible. |
| Firewall rules on the host for each tool container | Every protocol. | The worker would have to change the host's firewall, which needs root on the host itself, and rules left behind by a dead worker stay on the host. |
| A guard container that sets a firewall in its own network namespace, which the tool container then joins without the right to change it | Every protocol, checked by the kernel for each packet. Nothing on the host changes. The rules go away with the namespace. | Two containers per call. Rules are by address, not by name. |

The fourth was tried on Docker 29.1.3 with nftables. The guard had
`NET_ADMIN` and a policy of dropping all output except one address and port.
The tool container joined its namespace with no capabilities.

| Attempt from the tool container | Result |
|---|---|
| The allowed address and port | reached |
| Another container on the same network, an address on the internet, 169.254.169.254 | no answer |
| Removing the rules, as an ordinary user and as root | refused: operation not permitted |
| The same container on the same network without the guard | reached the address that was blocked before |

The guard's rule counted the dropped packets.

## Decision

The fourth option.

- A job's spec can carry a `scope`: a list of targets, each an address, an
  address range or a host name, with optional ports. The API refuses a job
  whose scope cannot be read. The scope is part of the job and cannot be
  changed by the job.
- A job without a scope gets no network for its tools.
- For a job with a scope, each sandboxed call gets a guard container of its
  own. The guard sets the rules and nothing else: drop all output and all new
  input, allow the job's targets, and before that refuse a fixed deny list
  that no scope can open. The deny list holds the link-local range with the
  cloud metadata address, and the networks of the runtime's own services.
- The tool container joins the guard's namespace with no capabilities, so it
  can neither change the rules nor leave the namespace.
- Host names are resolved by the worker, once per call. The tool container
  gets the result as fixed entries and has no resolver. The addresses are
  written into the call's `tool_intent` step. A name that resolves into the
  deny list is refused.
- After the call the worker reads the guard's counter of dropped packets and
  stores it with the `tool_result`.

## Consequences

- The model has no part in the check. A packet to an address outside the
  scope is dropped whatever the tool or the prompt says.
- The rules know addresses and ports. Two sites on one address cannot be told
  apart, and a scope of URL paths cannot be enforced here. That part of
  SE-006 is a gap.
- No wildcard names. A name is resolved when the call starts; what it
  resolves to later is not followed.
- A tool cannot look names up by itself. A job that needs a resolver names it
  as a target.
- The worker starts a container with `NET_ADMIN` for every scoped call. That
  is within what the socket already allows
  ([ADR 0008](0008-run-each-tool-call-in-its-own-container.md)).
- The host needs nftables in its kernel.
- At the time of this record only the trial above exists. The proof is
  planned as tests that must fail in two ways: with the rules removed, and
  with Docker's default settings.
