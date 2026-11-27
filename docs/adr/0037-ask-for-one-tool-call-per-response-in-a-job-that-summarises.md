# 0037: Ask for one tool call per response in a job that summarises

Status: accepted, 2026-11-27.

![Without a limit, one response asks for 36 file reads and the next request holds about 379K tokens, past the 20,000 trigger. With one call per response, each request adds one result, the trigger is checked before every call, and the summary step runs when it is reached.](../diagrams/one-call-per-response.svg)

## Context

A job with a summary trigger ([ADR 0033](0033-summarise-a-long-conversation-as-a-step.md))
checks the trigger between model calls. A model may ask for many tool calls
in one response, and all their results go into the next request.

In 10 jobs on `claude-haiku-5-5`, each asked for 36 file reads at once. The
next request held about 379,000 tokens against a trigger of 20,000, and no
job reached a summary ([issue 258](https://github.com/sindredg/agent-runtime/issues/258)).

## Options

| Option | For | Against |
|---|---|---|
| Ask the API for one call per response (`disable_parallel_tool_use`) | The API's own switch. Each request adds at most one result. | More model calls. The runtime must still check the answer. |
| Run only the calls that fit, answer the rest "call again" | Keeps parallel reads. | A new kind of tool result, more code to prove. |
| A rule in the job's prompt | No runtime change. | Rests on the model obeying. |

## Decision

- Every request of a job with a summary trigger, except the summary request,
  sends `tool_choice` `{"type": "auto", "disable_parallel_tool_use": true}`.
- The setting follows from the trigger the job's first request pinned, so a
  worker that takes over sends the same requests. A job with no trigger
  sends no `tool_choice`, as before.
- A response to such a request with two or more `tool_use` blocks fails the
  job before any tool runs. The reason is stored on the response step, so a
  worker that takes over fails it the same way.

## Consequences

- A request holds at most the trigger, one response and one tool result's
  limit. The trigger now does what ADR 0033 meant.
- A job that summarises makes one model call per tool call. It is slower and
  resends its context more often.
- A job begun by a worker without this change and taken over by one with it
  sends the setting from the takeover on.
- Jobs with no trigger are unchanged, so every result recorded for them stands.
