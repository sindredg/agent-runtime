# API

The API accepts jobs, serves them with their steps, and takes cancels and
approval answers. It holds no state. The code is `src/runtime/api/app.py`.

It has no authentication. The stack publishes it on `127.0.0.1` only: do not
publish its port on another interface.

A browser reaches localhost for any page it shows, so the API answers only
requests that name it by an allowed name (`API_ALLOWED_HOSTS`):

```mermaid
flowchart LR
    R[request] --> H{Host one of<br/>the allowed names?}
    H -->|no| X[400]
    H -->|yes| C{a change, with an Origin<br/>that is not the API's own?}
    C -->|yes| Y[403]
    C -->|no| OK[the route]
```

- A page that points its own name at 127.0.0.1 (DNS rebinding) sends that
  name in `Host`, and is refused.
- A form sent from another site's page, also one on another port of
  localhost, carries that site in `Origin`. Reads are left to the browser:
  the API sends no CORS header.
- IPv6 addresses are not accepted as names, so `[::1]` is refused.

The check is the package `packages/allowed_hosts/`. The fake provider and the
fake tracker run it too, with fixed names: their service name, `localhost`
and `127.0.0.1`.

## Routes

| Route | Returns |
|---|---|
| `POST /jobs` | 201 and the job. The body is a [job file](usage.md#job-files). |
| `GET /?status=&limit=&before=` | The [status page](#status-page): the same list as HTML |
| `GET /jobs?status=&limit=&before=` | Jobs newest first, at most `limit` (1 to 200, default 50). [Listing jobs](#listing-jobs) |
| `GET /jobs/{id}` | The job |
| `GET /jobs/{id}/steps?after=&limit=` | Steps with `seq` above `after`, in order, at most 1000 |
| `GET /jobs/{id}/events` | The steps as server-sent events |
| `POST /jobs/{id}/approve` | The job, now `queued`, after an `approval_given` step |
| `POST /jobs/{id}/deny` | The job, now `queued`, after an `approval_denied` step |
| `POST /jobs/{id}/cancel` | 200 or 202 and the job |
| `GET /healthz` | 200 while the process serves. It does not check Postgres. |

FastAPI also serves `/docs`, `/redoc` and `/openapi.json`.

## Listing jobs

- The order is by id. Ids are UUIDv7, made by the database, so newer jobs
  have larger ids. `created_at` can differ from that order by the length of
  a transaction.
- `before=<id>` returns the jobs older than that id. The last id of a page
  asks for the next one. A page shorter than `limit` is the last.
- An item has no spec and no lease; `GET /jobs/{id}` has them. A job in
  `needs_approval` has `question`: its number, tool and reason. The call's
  `input` field is left out; a reason can still quote it.
- A job inserted while a client pages can be missed: its id is made when the
  insert runs, and it is visible only once its transaction commits.
- Anyone who reaches the API can list every job. See
  [Limits](limits.md#api).

## Status page

`GET /` shows one page of the list as HTML, with a link to each job's
steps. It takes the list's parameters and loads again every 5 s.

- It changes nothing: no form, no script. Answer and cancel with the CLI.
- It is the API's only HTML. A script at the API's origin would pass the
  Origin check, so every value is escaped and the Content-Security-Policy
  allows only the page's own style. The code is `src/runtime/api/page.py`.

## Refusals

| Status | When |
|---|---|
| 400 | A `Host` that is not one of the allowed names |
| 403 | A change sent with an `Origin` that is not the API's own address |
| 404 | The job does not exist |
| 409 | An answer the job does not wait on, or a cancel of a job that succeeded or failed |
| 422 | A job id that is not a UUID, a parameter out of range, an unknown status, an unknown field, a missing question number |

- The body of approve and deny is `{"question": <seq>}`. See
  [Approval](approval.md#an-answer-names-its-question).
- The answers of the cancel route are in
  [Cancel, deadline and budget](halt.md#what-the-cancel-route-answers).
- `prompt` and `system` are limited to 1,000,000 characters each, `files` to
  1 MiB. The size of a request body is not capped.

## The event stream

```mermaid
sequenceDiagram
    participant C as client
    participant A as api
    participant D as postgres
    C->>A: GET /jobs/{id}/events
    loop until the job is final
        A->>D: status and the steps above the last seq, in one statement
        A-->>C: event step, id = seq, once per step
        opt the job stopped in needs_approval
            A-->>C: event status
        end
        opt 15 s without a step
            A-->>C: keep-alive comment
        end
    end
    A-->>C: event end, with the final status
```

- Status and steps are read in one statement. When the status is final,
  every step has been seen.
- A job that stops in `needs_approval` is not over. The stream says so once
  each time the job stops there, and stays open. A `notification_sent` step
  is sent as a step and is not a new stop.
- `Last-Event-ID` or `?after=` continues a broken stream. `Last-Event-ID`
  wins when both are given.
- The API polls the database for new steps. Nothing is pushed.
