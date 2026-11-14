# 0007: Build for security agents, and measure the controls against OWASP APTS

Status: accepted, 2026-11-14

## Context

The runtime keeps a long agent job alive through crashes and does not repeat
a tool call blindly. That says nothing about what the agent is allowed to do
while it runs.

The jobs this runtime is meant for are security agents: reconnaissance and
testing against targets the operator is authorised to test. An agent of that
kind has to stay inside its targets, ask before it does something that cannot
be undone, and leave a record that can be checked afterwards. A sentence in
the prompt does none of that: the model can ignore it, and text the agent
reads from a target can argue against it.

The OWASP Autonomous Penetration Testing Standard (APTS, version 0.1.0)
writes these controls down as requirements for a whole testing platform. It
asks, among other things, for a scope that is checked before every network
action, a sandbox and an allowlist enforced outside the model, approval
before irreversible actions, and a tamper-evident log.

## Options

| Option | For | Against |
|---|---|---|
| Stay general. Leave scope, approval and audit to whoever writes the tools | Nothing to build. | The controls then live in tool code or in the prompt, per tool, and nothing proves they hold. |
| Enforce the three controls in the runtime, defined in its own terms | The model cannot switch them off. | Nobody outside can tell what the controls are worth or what is missing. |
| Enforce them in the runtime and map them to the APTS requirements they answer | The same, and each control is measured against a public requirement with a number. Gaps show. | APTS is a draft and can change. Most of it is about a platform, not a runtime. |

## Decision

The third option.

- Three controls are enforced by the runtime, outside the model: the targets
  a job's tools can reach ([ADR 0009](0009-enforce-the-target-scope-in-the-network-namespace.md)),
  approval before a risky tool call
  ([ADR 0011](0011-ask-a-person-before-a-risky-tool-call.md)), and a log of
  every action.
- The documentation gets a table of the APTS requirements that concern a
  runtime. Each row names the requirement, what answers it here, and a
  status: tested, configured and not tested, or a gap.
- Only requirements of the runtime's own layer are in the table: scope,
  sandbox, halt, approval, the log. Requirements about operators, reporting,
  impact scoring and the organisation that runs a test are outside it.
- No conformance with APTS is claimed, for any tier.
- The runtime ships no testing tools. The sandbox image and the tools in it
  belong to the operator.

## Consequences

- The sandbox has to let a job reach its targets and nothing else. "No
  network at all" is not enough for these jobs.
- The table follows a named version of APTS. A new version means reading it
  again.
- A platform built on this runtime still has most of APTS to meet by itself.
- APTS does not ask that an action is not executed twice after a crash. That
  guarantee ([ADR 0005](0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md))
  stays, and is listed beside the table as an addition.
- At the time of this record the three controls are not built. Tools run in
  the worker process without a sandbox.
