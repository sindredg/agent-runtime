# Known limits

What the design accepts, and what is not built. Reasons: [ADRs](adr/).

## The main one: fencing protects the database, not the outside world

```mermaid
sequenceDiagram
    participant W1 as worker 1
    participant D as postgres
    participant T as tracker
    participant W2 as worker 2
    W1->>W1: lease check passes
    Note over W1: paused for longer than its lease
    W2->>D: takes the job over
    W2->>T: the check says not done, so the call runs again
    Note over W1: wakes up
    W1->>T: starts the one call it had checked for
    W1->>D: result
    D-->>W1: refused
    Note over T: the change was made twice, and no person was asked
```

A worker that passes its lease check and is then paused for longer than its
lease starts that one tool or model call when it wakes. Its result is
refused, but the tool ran. An idempotency key on the other side closes this.

## Durability

- The worker's lease deadline is on its monotonic clock, the lease itself on
  the database clock. A suspended machine, or a database clock set forward,
  lets one check pass on a lease that is over. The write is still refused.
- A job is failed at its sixth takeover, also when the workers were lost for
  reasons that have nothing to do with the job. The limit is a constant.
- A 429 or 529 and, up to a cap, an error that may pass are waited out.
  Other failed calls fail the job. Workers share no limit ([Backoff](backoff.md#limits)).
- A response cut off at `max_tokens`, or a refusal, fails the job. It is not
  continued.
- The whole conversation is sent with every model call. A job that outgrows
  the model's context window fails.
- All workers have to see the same workspace directory. The stack does that
  with one volume on one host.
- A workspace left behind waits up to `SWEEP_INTERVAL_SECONDS` for the sweep.
  A workspace root that no running worker mounts is not swept.

## Tool calls

- "Not done" from a tool's check is the tool author's claim, and so is the
  bound a tool names for its request. After a takeover `create_issue` waits
  12 s for its request. One the tracker stores later still makes two, and the
  reset is not tried over TLS ([ADR 0025](adr/0025-a-tool-in-the-worker-names-how-long-its-request-can-arrive.md)).
- On Gitea, the `create_issue` check misses an issue whose marker line a
  person edited or wrote below, and is fooled by a bot issue edited to end
  with another call's marker. It trusts that each page's `X-Total-Count`
  counts the list that page came from, and answers unknown past 1,000 issues
  by its user ([ADR 0019](adr/0019-find-a-gitea-issue-by-a-marker-line-not-by-search.md)).
- The operation id protects a call the runtime runs again. A model that is
  told a call failed and asks again makes a new call with a new id.
- A tool in the worker process that is running when its worker loses the
  lease, or when its job is halted, runs to its end. Its result is dropped.
- `read_file`, `crash_worker` and the lookup of `create_issue` have no general
  timeout. One that outlasts the stop grace period is killed with its worker.
- A worker told to stop while it writes a `tool_intent` still runs the tool.
- A worker that takes over an interrupted call of a tool it does not offer
  stores "unknown tool". The call may have run. Run every worker of a
  deployment with the same tools.
- The wait before a decision about an interrupted sandboxed call assumes the
  worker was not paused before the start request, and that Docker does not
  act on a request late. A container that starts later can still be running.

## Sandbox and scope

- The worker holds the Docker socket. Whoever controls the worker controls
  the host. Tool containers share the host's kernel.
- The workspace limit is soft, and some writes are not seen at all: a file
  deleted while it is held open, a large `fallocate`. One call can still
  fill the disk that holds the Docker volumes, Postgres among them. The hard
  limit is [issue 78](https://github.com/sindredg/agent-runtime/issues/78).
- A command can also write to `/dev/shm` (counted against its memory) and
  `/dev/mqueue`. Neither outlives the call.
- A tool container whose removal failed, or was cut off when its worker
  exited, stays on the host stopped. The sweep removes workspaces only
  ([issue 45](https://github.com/sindredg/agent-runtime/issues/45)).
- A container whose `create` was cut off is removed by name. If Docker had
  not created it yet, it is left in the `created` state, without running.
- What a scope does not cover is on [Scope](scope.md#limits).
- `read_file` checks paths and symlinks, not hard links. Nothing built so far
  can create a hard link out of a workspace. It checks the file it opened,
  so a link swapped in after the path check is refused too.
- Before a workspace is removed, its directories get mode 700. A link swapped
  in during that walk is followed. Its target is the worker's own, and gets
  the same mode.

## Secrets

- [A test](../tests/integration/test_secret_canary.py) follows the provider key
  and the tracker token through a job. Not covered: a secret in a job's files
  or a tool's output, text written past the logger, the database password, `NOTIFY_URL`.

## Halt and budget

- `token_budget` is not a hard bound. A model call that is cut off can be
  billed and is not counted. A job can be taken over 5 times, and can make
  up to 37 failed calls to a provider that errors ([Backoff](backoff.md#the-caps)).
- The bound counts tool results by their bytes, a token for each. A job with
  large tool results stops well before its budget is spent.
- The bound assumes a token is never shorter than one byte. Tokens a provider
  reports under other names, such as prompt cache reads, are not counted.
- A worker with more tools than the one before it relies on the 1024
  allowance to cover the difference in tool definitions.
- The [repeat stop](halt.md#a-job-that-repeats-itself) misses a loop over two or more calls, an input
  that changes a little each time, and a result that changes on its own, such
  as a time stamp. A poll that sees the same answer 5 times is stopped too.
- A halt waits for what it cannot race: container removals and the fenced
  write that ends the job. A slow Docker daemon can push a cancel or a
  deadline past 5 s.
- A cancel answered 202 can still end `succeeded` or `failed` as the job ends.
- The deadline timer reads the database clock once, then counts on the
  monotonic clock. A wall clock step while it waits moves the stop by as much.
- The 5 s a stop leaves around a sandboxed call is a margin, not a bound. A
  slow database can push a stop past `STOP_GRACE_SECONDS`.

## Approval

- The gate is per tool, not per call. With `run_shell` named, a command that
  lists a directory asks as loudly as one that deletes it.
- A job that names no tool is asked only about an interrupted call that
  nobody can vouch for, such as an interrupted `run_shell` call.
- A waiting job waits until a person answers or cancels, or until its
  deadline. Entering a call's result by hand is not built.
- The log records that a call was approved or denied, not by whom.
- A question is posted at least once: a kill, a lost answer or a stall past
  the claim's 12 s repeats it. A worker posts one at a time, so a hanging
  receiver delays the rest by up to 10 s each. No authentication; a token in
  an `http` URL crosses the network in clear. Each park is one POST, not capped.

## API

- No authentication. Anyone who can reach the API can submit, list, approve,
  deny and cancel. That includes every local user and process of the host.
- The API and the fakes refuse a foreign `Host` and a change from another
  site's page. A local program sends any header and can use every fake.
- Postgres is published on `127.0.0.1` with default passwords for both
  [roles](database-roles.md). A local process can write jobs and steps
  directly, past the fence and the gate. As the owner it can switch the
  [chain's](step-log-chain.md) triggers off and rewrite the log.
- Anyone who reaches the API can list every job, also on the status page,
  with its error and open question's reason, both text a target can write. A listed id gives the
  job's spec and every step.
- The size of a request body is not capped. Only the fields are limited.
