# 0003: Call the model through the official SDK, and test against a fake that speaks the same wire format

Status: accepted, 2026-11-13

## Context

The worker calls the Anthropic Messages API many times per job. Tests and
load runs must not call the real API: it costs money, it is not
deterministic, and it cannot be told to fail on demand. So there are two
questions. What does the worker use to call the API, and what do tests call
instead?

## Options

| Option | For | Against |
|---|---|---|
| Official `anthropic` SDK, and a fake HTTP server in the Messages format | The SDK parses the event stream and maps errors to types, and it follows API changes. The SDK parsing the fake's responses is a check of the fake that does not depend on this project's reading of the API. Tests exercise real HTTP and real streaming. | One more runtime dependency. The fake has to be kept true to the API by hand. |
| Raw HTTP client, and the same fake server | No new dependency, full control of the stream. | The client and the fake would both come from one reading of the API. A shared mistake stays hidden until a call to the real API. |
| Official SDK, and a stub provider object inside the worker for tests | No fake server to maintain. | Tests skip HTTP, streaming, timeouts and error statuses, which is where a provider client fails. Nothing to point a load test at. |

## Decision

The worker uses the official SDK behind a small `Provider` interface. Tests
and the local stack use a fake provider: a separate service that answers
`POST /v1/messages` from scenario files. Which one the worker calls is one
setting, the base URL.

Two parts of the SDK's default behaviour are switched off or guarded:

- Retries are off (`max_retries=0`). The runtime decides about retries, so
  every attempt is a step in the log.
- A stream that ends without a stop reason is an error. The SDK returns what
  it collected so far; the client refuses it.

The fake picks a scenario by the request's `model` and a turn by the number
of assistant messages in the request. The same request always gets the same
answer, which a resent request after a failure needs. It keeps a log of every
request, and tests read that log as the record of what was really sent.

## Consequences

- The fake can drift from the API. A contract test sends the same exchange to
  both and compares the response shapes. It needs a key and is run by hand
  when the client or the fake changes. At the time of this record it has not
  been run.
- The fake checks requests only as far as it is written to: required fields,
  and that tool calls and tool results pair up one to one. A request it
  accepts can still be refused by the API.
- Scripted answers do not depend on tool output. A scenario cannot show that
  the model would have reacted to a result.
- The fake has one mode, healthy. Slow, rate-limited and unavailable modes are
  planned.
