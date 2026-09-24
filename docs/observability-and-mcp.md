# Observability and MCP

Laya Studio records what it does — every log line, every request, every traceback —
and then exposes that record to two callers that are not a browser: HTTP, and an
agent over the **Model Context Protocol**.

There are two halves, and they solve the same problem from opposite ends:

| Half | Question it answers |
|---|---|
| **The log store** | "What happened, and where is the evidence?" |
| **The MCP server** | "Can something other than a person reach the panel and act on that evidence?" |

Together they make the panel *debuggable while it is running*: an agent can
evaluate a state, watch it fail, read the traceback that failure wrote, find the
row behind it, and fix the schema — without anyone copying a stack trace into a
chat window.

```bash
make logs      # the last 20 records, from the running panel
make errors    # distinct exceptions, grouped by raise site
```

---

## Part 1 — The log store

### Why a file

The panel is deployed as one container and one PostgreSQL. The questions you
actually ask while debugging one are "what happened in the last few minutes" and
"has this traceback happened before". A rotated JSONL file answers both:
`grep` answers it from a shell, `GET /api/observability/logs` answers it over
HTTP, and the MCP tools answer it for an agent. Nothing extra to run, nothing
extra to secure, and it survives a crash — which is precisely when you need it.

The file is **bind-mounted out of the container** (`./data:/app/data` in
`docker-compose.yml`) so that reading it does not require `docker exec`:

```bash
grep '"level": "ERROR"' data/logs/*.jsonl
tail -f data/logs/laya-studio-*.jsonl
```

### What a record looks like

One JSON object per line, and every record has `ts`, `kind`, `level`, `logger` and
`message`. Three kinds:

| `kind` | Written by | Carries |
|---|---|---|
| `log` | any `logging` call | `module`, `func`, `line`, `thread`, `process`, plus whatever was passed via `extra=` |
| `request` | the request middleware | `method`, `path`, `query`, `status`, `duration_ms`, `request_id`, `client` |
| `exception` | the middleware, or one of the process hooks | as `request`, plus `exc` |

`exc` is the part worth having:

```json
{
  "type": "ValueError",
  "message": "Row 12 has no 'state' column",
  "location": "/app/app/services/bulk.py:184",
  "fingerprint": "9f2c1ab0d4e77c31",
  "traceback": "Traceback (most recent call last): ..."
}
```

**`fingerprint` is the trick that makes a log readable.** It is a hash of the
exception type and the raise site — *not* of the message, because the message is
usually the thing that varies between occurrences (a row number, an id, a DSN).
That is what lets `errors_list` say "one `ValueError` in the bulk parser, 4 000
times" instead of listing 4 000 records.

```bash
curl -s 'localhost:8000/api/observability/errors' | jq '.groups[] | {type, location, count, last_seen}'
```

### Requests and correlation

The middleware gives every request a `request_id`, writes it into a response
header (`X-Request-ID`) and binds it to the logging context, so **every log line a
request produces carries it**:

```json
{"kind": "request", "request_id": "7c1f...", "method": "POST",
 "path": "/api/workflows/3/run", "status": 500, "duration_ms": 812.4}
{"kind": "log", "level": "ERROR", "logger": "app.services.actions",
 "message": "webhook failed: Connection refused",
 "request": {"request_id": "7c1f...", "method": "POST", "path": "/api/workflows/3/run"}}
```

So one id walks the whole story: quote it from a browser, `logs_search(request_id=...)`
from an agent, `grep` it on the host.

Two deliberate choices inside the middleware:

* **It never reads a request body.** Logs that archive payloads turn an evaluation
  of a customer email into a second copy of that email on disk. Everything
  diagnostic — path, status, duration, correlation id, traceback — is available
  without it.
* **It is written against raw ASGI, not `BaseHTTPMiddleware`.** The MCP endpoint can
  answer with `text/event-stream`, and a middleware that buffers a response in order
  to inspect it would hold that stream open until it finished, which for SSE is
  "never".

### Rotation, retention and multi-process safety

| Setting | Default | Meaning |
|---|---|---|
| `LAYA_STUDIO_LOG_DIR` | `<data dir>/logs` | Where the files live. Bind-mounted in Docker. |
| `LAYA_STUDIO_LOG_MAX_MB` | `8` | Per file, before it rotates. |
| `LAYA_STUDIO_LOG_BACKUP_COUNT` | `5` | Rotated files kept per process. |
| `LAYA_STUDIO_LOG_RETENTION_DAYS` | `14` | Older rotated files are deleted. |
| `LAYA_STUDIO_LOG_REQUESTS` | `true` | Record every request. |
| `LAYA_STUDIO_LOG_JSONL` | `true` | `false` = stderr only, nothing on disk. |

A file is rotated *before* the write that would overflow it, so the live file
always exists — `tail -f` on the host never loses its target. The filename carries
the pid (`laya-studio-4242.jsonl`), so two uvicorn workers, or a `laya-studio`
CLI command running alongside the panel, never interleave a line.

### What it is not

* **Not a metrics pipeline.** No aggregation, no shipping, no sampling.
* **Not durable history.** The window is bounded by rotation and retention. An
  event you needed last month is gone.
* **Not a place for request bodies.** See above; the omission is the design.

Every read reports how much of the window it actually looked at (`scanned`) and
whether the answer was cut short (`truncated`). A bare empty result is
indistinguishable from "the log rotated", which is the wrong thing to conclude
while debugging.

---

## Part 2 — The HTTP surface

Read-only, under `/api/observability`, 200 for a read and 401 without the token
when one is set.

| Endpoint | Returns |
|---|---|
| `GET /api/observability/logs` | Filtered records. `level` is a **floor** (`WARNING` = WARNING and above); also `kind`, `logger`, `q` (substring), `request_id`, `path`, `status_min`/`status_max`, `has_exception`, `fingerprint`, `since`, `until`, `limit`, `order`, `include_traceback` |
| `GET /api/observability/logs/tail?lines=50` | The most recent records of any kind — start here |
| `GET /api/observability/errors?since=&limit=&include_traceback=` | Exceptions grouped by fingerprint, newest first |
| `GET /api/observability/errors/{fingerprint}` | One group's occurrences, tracebacks included |
| `GET /api/observability/requests?status_min=&path=&limit=` | The request timeline |
| `GET /api/observability/stats` | Files, sizes, counts by level and kind, oldest/newest, retention settings |

Tracebacks are opt-in and capped at 12 000 characters (the tail is kept, and
`traceback_truncated` is set) so a broad search stays cheap to move around.

```bash
# What has been failing?
curl -s 'localhost:8000/api/observability/requests?status_min=500' | jq '.records[0]'

# Everything one request did, log lines included
curl -s 'localhost:8000/api/observability/logs?request_id=7c1f...&include_traceback=true'
```

**Set `LAYA_STUDIO_OBSERVABILITY_TOKEN` before exposing the panel.** These endpoints
return log messages and tracebacks, and a traceback carries whatever the failing
code had in hand. Unset, they are open — the right default for a panel on your own
machine, and the wrong one anywhere else.

---

## Part 3 — The MCP server

### What it is

The panel publishes its own tool surface at **`/mcp`** using the Model Context
Protocol's Streamable HTTP transport: **60 tools**, covering the whole JSON API plus
the log store and read-only SQL. An agent can then drive the panel and debug it
over one protocol.

### Why it runs inside the panel

This is the design decision that shapes everything else
(see [ADR 0013](adr/0013-mcp-inside-the-panel.md)):

* The engine takes ~2 minutes to load and holds several GB resident. A sidecar MCP
  server that answered `evaluate` would either load a *second* copy or talk to the
  panel over HTTP anyway.
* The panel already has the database session factory, the settings and the log
  store. Running in-process, a tool call **is** a request to the panel: it goes
  through the same middleware, gets a correlation id, and lands in the same log —
  which is what makes the observability tools honest, because the agent's own calls
  are in the log it is reading.

The transport is **stateless** with JSON responses: every POST stands alone. No
session id to lose, no state to rebuild after a reconnect or a reload, and a client
that does not echo `Mcp-Session-Id` still works.

### Using it

The endpoint is live whenever the panel is. A `.mcp.json` at the repository root
already registers it for this project, which Reasonix, Claude Code, VS Code and
anything else that reads the `mcpServers` spec will pick up:

```json
{
  "mcpServers": {
    "laya-studio": { "type": "http", "url": "http://127.0.0.1:8000/mcp" }
  }
}
```

The equivalent entry in `~/.reasonix/config.toml` (or a project `reasonix.toml`)
looks like this:

```toml
[[plugins]]
name                 = "laya-studio"
type                 = "http"
url                  = "http://127.0.0.1:8000/mcp"
call_timeout_seconds = 600   # a bulk job or a workflow run can take minutes
```

Any other Streamable HTTP client takes the same URL. For a **stdio-only** client
(most desktop clients), run the standalone server against the panel instead — it is
the same tools over the same code:

```bash
python -m app.mcp                        # stdio, toward http://127.0.0.1:8000
python -m app.mcp --base-url https://panel.example.com
python -m app.mcp --transport http --port 8765   # a second HTTP endpoint
laya-studio mcp                          # the same program, via the CLI
```

Standalone mode does **not** load the engine: the panel does the work and this
process is a thin client. `db_query` is the exception — it needs its own database
connection, so set `LAYA_STUDIO_DATABASE_URL` for it.

### The tools

Grouped the way the panel is. Reads are annotated `readOnlyHint`; deletes, workflow
runs, ingestion and review retries are `destructiveHint`, so a client can gate or
confirm them.

| Area | Tools |
|---|---|
| Observability | `logs_search`, `logs_tail`, `log_stats`, `errors_list`, `errors_detail`, `request_log`, `db_query` |
| Health | `health`, `readiness` |
| Decisions | `evaluate`, `route`, `lint_questions`, `evaluation_history`, `evaluation_get`, `evaluation_label` |
| Schemas | `schemas_list`, `schema_get`, `schema_create`, `schema_update`, `schema_delete`, `schema_fork`, `schema_lint`, `schema_revisions` |
| Bulk | `bulk_upload`, `bulk_jobs`, `bulk_job_get`, `bulk_job_items`, `bulk_job_rerun`, `bulk_job_delete` |
| A/B | `ab_test_create`, `ab_tests`, `ab_test_get`, `ab_test_run`, `ab_test_delete` |
| Workflows | `workflows_list`, `workflow_get`, `workflow_create`, `workflow_update`, `workflow_delete`, `workflow_run`, `workflow_runs`, `workflow_run_get`, `workflow_stats` |
| Review | `review_items`, `review_item_get`, `review_claim`, `review_resolve`, `review_retry`, `review_stats` |
| Analytics | `analytics_overview`, `analytics_dashboard`, `analytics_timeseries`, `analytics_routes`, `analytics_calibration`, `calibration_report`, `calibration_refit`, `analytics_thresholds` |
| Ingestion | `ingest`, `ingest_text`, `ingest_email` |

Three properties worth knowing as a caller:

* **A tool call is an API call.** The tools go through the documented JSON API
  rather than reaching into the service layer, so there is one implementation of
  "create a schema" and a tool cannot drift from
  [api-reference.md](api-reference.md) without a test noticing. Errors come back as
  the panel's own message (`404: No schema 999999`), not a generic failure.
* **Every tool returns one JSON object.** Endpoints that answer with an array are
  wrapped as `{"count": n, "items": [...]}`. The SDK would otherwise expand a list
  into one content block per element, which makes an empty list indistinguishable
  from no content at all.
* **MCP-made requests are attributed.** Tools set `X-Laya-MCP-Tool`, which the
  middleware records as `mcp_tool`, so `logs_search(q="evaluate")` tells you the
  agent asked, not the UI.

`db_query` deserves its own sentence: it runs **one** statement, inside
`SET TRANSACTION READ ONLY`, with a 10s statement timeout and a row cap. Only
`SELECT`/`WITH`/`TABLE`/`VALUES`/`SHOW`/`EXPLAIN` openers reach the database, string
literals and comments are stripped before the keyword scan (so `WHERE note =
'delete me'` is not mistaken for a DELETE), and PostgreSQL itself refuses a write
that slips past the scan. Schema changes belong in a Prisma migration; data changes
belong in the API.

### Security

The MCP endpoint can create, run and delete the panel's data, so it is treated like
the ingestion surface rather than like a page:

| Setting | Default | Meaning |
|---|---|---|
| `LAYA_STUDIO_MCP_ENABLED` | `true` | Serve `/mcp` at all. |
| `LAYA_STUDIO_MCP_TOKEN` | *(unset)* | When set, requests need `Authorization: Bearer <token>` or `X-MCP-Token`. |
| `LAYA_STUDIO_MCP_ALLOWED_HOSTS` | *(empty)* | Extra `Host` headers to accept. |
| `LAYA_STUDIO_MCP_BASE_URL` | `http://127.0.0.1:8000` | What a *standalone* server talks to. |
| `LAYA_STUDIO_MCP_TIMEOUT` | `300` | Seconds allowed for one tool call. |

**DNS-rebinding protection stays on.** Loopback (`127.0.0.1`, `localhost`, `[::1]`)
and the container name are always accepted; anything else has to be declared in
`LAYA_STUDIO_MCP_ALLOWED_HOSTS`, because the alternative is a web page the user did
not intend to give this much power to reaching the endpoint through a rebound
hostname. Requests from an undeclared host get **421**.

**Unset token means an unauthenticated endpoint.** The panel logs a warning at
startup outside `development`. Set the token, or turn the endpoint off, before
exposing the panel to anything but localhost.

---

## Debugging recipes

**"A decision is failing."**

1. `health` — `starting` means the checkpoints are still loading and the 503 is
   correct behaviour, not a bug. `readiness` gives the same answer as a status.
2. `logs_search(level="WARNING", limit=50)` — the ladder and the engine both log
   why they refused.
3. `errors_list` → `errors_detail(fingerprint)` — one group, tracebacks included.

**"Something a while ago went wrong."**

`logs_search(kind="exception", since="2026-09-22T09:00:00Z", include_traceback=true)`
bounded the window; `errors_list(since=...)` groups it; `request_log(status_min=500)`
shows the requests that produced it.

**"What did that request touch?"**

Take the `request_id` (from the response header, the UI, or `request_log`) and
`logs_search(request_id=...)`. Every log line the request produced carries the same
id.

**"The numbers look wrong."**

`analytics_calibration` for ECE and the reliability diagram, then `db_query` if the
API does not answer the question directly:

```sql
SELECT source, count(*), avg(confidence) FROM evaluations GROUP BY source ORDER BY 2 DESC
```

Try it in the UI at `/analytics` — and read
[analytics-and-calibration.md](analytics-and-calibration.md) before acting on any
number in it.

---

## Where the code lives

```
app/observability/
  store.py       the JSONL store: rotation, retention, queries, error grouping
  records.py     record shaping, fingerprints, timestamps
  middleware.py  raw-ASGI request logging and exception capture
  handler.py     the logging handler bridge
  hooks.py       sys / threading / asyncio exception hooks, warnings capture
  context.py     the request-scoped contextvar that follows a log line out
app/routers/observability.py   the read-only HTTP surface
app/mcp/
  server.py      the MCPServer, the /mcp endpoint, the token gate, the lifespan
  panel.py       how a tool reaches the panel (in-process ASGI, or HTTP)
  tools/         the tools, one module per area
  __main__.py    the standalone runner (stdio / http)
tests/test_observability.py    the store, the capture, the API
tests/test_mcp_server.py       the handshake, the tool surface, the tools
```

Two failure modes are handled on purpose, because they are the ones that lose the
evidence you wanted:

* **The engine loader thread dies** (`threading.excepthook`) or **a task fails with
  nobody awaiting it** (the asyncio loop handler). "The model never became ready" is
  otherwise a silent state.
* **The process dies on startup** — an unreachable database, or an unmigrated
  schema. `configure_logging` wires the store *before* anything else runs, so those
  startup failures are on disk too.
