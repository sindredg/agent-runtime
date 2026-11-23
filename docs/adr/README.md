# Decision records

One record per real decision: context, options, decision, consequences. An
accepted record keeps its text. A change is a new record.

## How a worker takes over a job

![Recovery decisions in reading order: claim the job (0002), fence the old worker’s writes (0004), and handle a tool call with no result (0005).](../diagrams/recovery-decisions.svg)

Read [the queue](0002-postgres-as-the-job-queue.md),
[the write fence](0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md) and
[interrupted tool calls](0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md) in that order.

## All records

| Record | Decision | Built |
|---|---|---|
| [0001](0001-build-durability-on-postgres.md) | Build job durability on Postgres instead of adopting a workflow engine | Yes |
| [0002](0002-postgres-as-the-job-queue.md) | Use Postgres as the job queue | Yes |
| [0003](0003-official-sdk-and-a-wire-compatible-fake.md) | Call the model through the official SDK, and test against a fake that speaks the same wire format | Yes |
| [0004](0004-fence-writes-with-the-lease-epoch-and-a-row-lock.md) | Fence a worker's writes with the lease epoch and a row lock, and accept one gap | Yes |
| [0005](0005-rerun-an-interrupted-tool-call-only-when-it-is-safe.md) | Rerun an interrupted tool call only when that is known to be safe. Otherwise ask a person. | Yes |
| [0006](0006-count-takeovers-apart-from-claims.md) | Count takeovers apart from claims, and release the job when a worker is told to stop | Yes |
| [0007](0007-security-agents-and-owasp-apts-as-the-yardstick.md) | Build for security agents, and measure the controls against OWASP APTS | [The APTS table](../apts.md) has the milestone 2 rows |
| [0008](0008-run-each-tool-call-in-its-own-container.md) | Run each tool call in its own container, started by the worker | For `run_shell` and `list_dir` |
| [0009](0009-enforce-the-target-scope-in-the-network-namespace.md) | Enforce a job's target scope in the network namespace of its tools | For `run_shell` |
| [0010](0010-one-halt-path-and-a-budget-bound.md) | Halt a job through one path, and bound a model call's tokens before it is sent | Yes |
| [0011](0011-ask-a-person-before-a-risky-tool-call.md) | Ask a person before a risky tool call runs | Yes |
| [0012](0012-an-answer-names-its-question.md) | An answer names the question it answers | Yes |
| [0013](0013-chain-the-step-log-and-split-the-database-roles.md) | Chain the step log with a hash, and write it as a role that cannot switch the chain off | [The two roles](../database-roles.md), [the chain](../step-log-chain.md) and `runtime verify` |
| [0014](0014-limit-a-jobs-workspace-with-a-watcher.md) | Limit a job's workspace with a watcher, and accept an overshoot | [Yes](../sandbox.md#workspace-size) |
| [0015](0015-keep-the-chains-trigger-to-the-owner-and-the-steps-table.md) | Keep the chain's trigger to the owner and the steps table | Yes |
| [0016](0016-verify-reads-the-database-not-the-api.md) | `runtime verify` reads the database, not the API | Yes |
| [0017](0017-bound-the-workspace-walk-and-state-what-it-cannot-see.md) | Bound the workspace walk, and state what it cannot see | Yes |
| [0018](0018-wait-out-a-busy-provider-in-the-waiting-status.md) | Wait out a busy provider in the `waiting` status | [Yes](../backoff.md), for one worker. No shared limiter |
| [0019](0019-find-a-gitea-issue-by-a-marker-line-not-by-search.md) | Find a Gitea issue by a marker line, not by search | [Yes](../lab-demo.md), in the lab demo |
| [0020](0020-run-recorded-kill-and-api-runs-in-a-manual-workflow.md) | Run recorded kill runs and API runs in a manual workflow | [Yes](../results/kill-runs.md#runs), run on the fake provider and three models |
| [0021](0021-run-switched-off-runs-from-a-reviewed-list.md) | Run switched-off runs from a reviewed list in the Manual runs workflow | Yes, [34 entries run](../results/switched-off.md) at `e1656b5` |
| [0022](0022-run-switched-off-entries-on-the-claude-api-from-the-list.md) | Run switched-off entries on the Claude API from the reviewed list | Yes, [run once](../results/kill-runs.md#with-the-stored-signature-changed) on `claude-fable-5-2` |
| [0023](0023-a-running-job-has-a-lease.md) | A `running` job has a lease, and the database refuses one without | Yes, migration 0009 |
| [0025](0025-a-tool-in-the-worker-names-how-long-its-request-can-arrive.md) | A tool in the worker names how long its request can still arrive, and a takeover waits that long | [Yes](../interrupted-calls.md#before-the-decision-a-tool-in-the-worker), for `create_issue` |
| [0026](0026-python-on-asyncio-with-the-sql-written-out.md) | Write the runtime in Python on asyncio, with the SQL written out | Yes |
| [0027](0027-an-api-without-authentication-on-localhost.md) | The API has no authentication and answers on localhost only | Yes |
| [0028](0028-send-the-whole-conversation-and-fail-past-the-context-window.md) | Send the whole conversation every time, and fail a job that outgrows the context window | Yes |
| [0029](0029-notify-from-a-row-the-park-writes.md) | A parked job notifies from a row its park writes, at least once | Yes |
| [0030](0030-wait-out-a-provider-error-up-to-a-cap.md) | Wait out a provider error that may pass, up to a cap | [Yes](../backoff.md#the-caps) |

There is no record 0024: it was withdrawn before anything was built.

Each record states what was built at the time it was written. What is built
now is described in [Architecture](../architecture.md).
