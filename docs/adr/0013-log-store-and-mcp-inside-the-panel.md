# 0013 — The log store is JSONL files, and the MCP server runs inside the panel

**Status:** Accepted

## Context

Laya Studio is a decision system, and the questions asked of it are the kind that
need evidence: *why did this item escalate, was the engine even ready, what did
that workflow actually do at 09:14?* Until now the only answer was a scrollback on
whatever terminal ran `docker compose up`, already rotated past the interesting
part, with no way to correlate a 500 with the log lines it produced.

Two facts shaped the fix:

**1. The evidence is cheap to keep and expensive to lose.** The panel already logs
the interesting parts. What was missing was persistence, a correlation id, and
tracebacks for the requests that raise — a request that returns 500 through the
middleware leaves no traceback anywhere in the panel's own output.

**2. The engine cannot be duplicated.** The checkpoints take **113.8 seconds** to
load in this deployment (measured: `startup_seconds` on `/api/health`) and hold
several GB resident. Any debugging surface that wanted to call `evaluate` would
either pay that cost twice or proxy back to the panel anyway.

And the intended user of that surface is increasingly not a person typing into a
terminal. An agent that can reach the panel — evaluate a state, watch it fail, read
the traceback, inspect the schema row behind it — closes a loop that otherwise
starts with someone copying a stack trace into a chat.

Two proposals were rejected early. A **log service** (Loki, Elastic) adds a second
thing to deploy and secure in order to answer "what happened ten minutes ago".
An **MCP sidecar** either loads a second copy of the engine or reimplements the
panel's API client-side.

## Decision

**Record everything to rotated JSONL, and publish a tool surface from inside the
panel itself.**

1. **`app/observability/`** writes one JSON object per line to
   `<data dir>/logs/laya-studio-<pid>.jsonl`: `log`, `request` and `exception`
   records, rotated by size before the overflowing write, retained by age, one file
   per process so two workers never interleave a line. Rotation and retention are
   bounded; this is a debugging window, not history.
2. **Exceptions get a fingerprint** — a hash of the exception type and the raise
   site, deliberately *not* of the message. Grouping is what turns 4 000 failed
   rows into one entry with a count, which is the difference between a log you can
   read and a log you cannot.
3. **A pure-ASGI middleware** assigns a `request_id`, returns it as
   `X-Request-ID`, binds it to a `contextvar` so every log line the request
   produces carries it, and records the status and duration. It never reads a
   request body: logs that archive payloads turn an evaluation of a customer email
   into a second copy of that email on disk.
4. **Three doors onto the same records**: the files (bind-mounted out of the
   container, so `grep` works), the read-only
   `/api/observability/*` endpoints, and the MCP tools.
5. **`app/mcp/` mounts an MCP server at `/mcp`** on the panel's own FastAPI
   application, Streamable HTTP, **stateless**, JSON responses. 60 tools mirroring
   the whole JSON API plus the log store and read-only SQL.
6. **The tools call the panel's documented HTTP API**, in-process over an ASGI
   transport (or over the network in standalone mode) rather than reaching into the
   service layer. There is one implementation of "create a schema" and a tool
   cannot drift from [api-reference.md](../api-reference.md) unnoticed.
7. **`db_query` is read-only by construction**: one statement, inside
   `SET TRANSACTION READ ONLY`, with a statement timeout and a row cap, and a
   keyword scan performed on a copy with literals and comments stripped (so
   `WHERE note = 'delete me'` is not mistaken for a DELETE).
8. **The token gate and DNS-rebinding protection are on by default in shape**: the
   endpoint can create, run and delete the panel's data, so it is treated like the
   ingestion surface — optional bearer token
   (`LAYA_STUDIO_MCP_TOKEN`), loopback-only `Host` headers unless declared, and a
   startup warning outside `development` when no token is set.

## Consequences

Measured on this machine:

| | |
|---|---|
| `LogStore.write` | **38.5 µs** per record |
| A tool call with no database work, end to end | **0.5 ms** median |
| The same call through the panel's own API | 0.4 ms median |
| Tools registered | **60** |
| New tests | 44 (`test_observability.py`, `test_mcp_server.py`) |

**Good.**

* The panel is now diagnosable while it is running, by three callers with the same
  data: a shell, an HTTP client, and an agent. `make logs` and `make errors` are
  one command each.
* A failure carries its own context: exception records reference the request that
  produced them, and log lines reference the request that produced *them*.
* An agent can act on what it finds — fix a schema, re-run a bulk job, resolve a
  review item — over the same protocol it used to diagnose the problem.
* Tool calls exercise the panel's real request path, so the agent's traffic is
  visible in the log with `mcp_tool` attribution. The observability tools read a log
  that includes the agent's own calls, which is what makes them trustworthy.
* The store cannot take the panel down: a write failure (full disk, read-only
  mount, permissions) is swallowed, and the stderr handler is unaffected.

**Bad.**

* **The default is an unauthenticated endpoint with destructive tools.** It is right
  for a panel on localhost and a real hazard on anything else; the mitigation is a
  startup warning and a documented token. A future default could flip this by
  environment, at the cost of "it just works" for the primary use case.
* **60 tools is a real context cost.** An agent that lists the surface pays for 60
  descriptions before it does anything. The mitigation is honest annotation
  (`readOnlyHint`/`destructiveHint`) and one-line descriptions; the alternative, a
  smaller curated surface, would have meant deciding in advance which questions are
  worth asking.
* **`LAYA_STUDIO_MCP_ALLOWED_HOSTS` is a thing to get right.** Reaching the panel by
  a name other than localhost requires declaring it, or the request is refused with
  421 and the reason is only in the response body.
* **Log volume roughly doubles** with `LAYA_STUDIO_LOG_REQUESTS=true`, since every
  request is now a record as well as (for uvicorn) a line of access log. The
  access log is suppressed into the store for exactly this reason.
* **A traceback is not a diagnosis.** The store makes failures findable and legible;
  it does not make them understood.

**Neutral.**

* The window is bounded by rotation and retention (8 MB × 6 files, 14 days by
  default), so this is not an audit trail and must not be sold as one.
* Requests that a client aborts (`CancelledError`) are not recorded as exceptions:
  they are not the panel's failures. `except Exception` rather than
  `except BaseException` is the mechanism.
* `/api/health` and `/static` are excluded from request records. The container's own
  healthcheck would otherwise write a record every 30 seconds forever.
* The MCP session manager is **single-use per instance**, so the transport is
  rebuilt in the lifespan and the route points at it through an indirection. This
  was not a design choice so much as a fact about the SDK that had to be handled —
  without it, the second `TestClient` in the suite raises, and a development reload
  would too.

## Alternatives considered

**A logging service (Loki / OpenSearch / a hosted log product).** Rejected: a second
deployment to run and secure so that "what happened ten minutes ago" can be
answered, when a JSONL file already answers it, works from a shell, and survived the
crash you are investigating.

**Ship logs to a file only, no HTTP surface.** Rejected: the panel runs in a
container, and "get me the logs" should not mean `docker cp` or `docker exec`. The
files stay the source of truth; the endpoints make them reachable.

**An MCP sidecar process.** Rejected for the engine reason above (see
[ADR 0009](0009-laya-is-the-only-engine.md) for why there is no cheap engine path),
and because a sidecar would recreate the panel's client and its error handling in a
second place.

**Mount the MCP app as a Starlette sub-application at `/mcp`.** Rejected: Starlette
answers `/mcp` with a 307 to `/mcp/`, and a client that POSTs without following
redirects fails in a way that looks like a broken server. Registering the route
directly keeps the path exact. The sub-application's own middleware is empty in this
configuration, so nothing is skipped.

**Stateful (session-based) MCP transport.** Rejected: it requires the client to echo
`Mcp-Session-Id`, which turns a reconnect, a reload or a client with a stricter
reading of the spec into a broken endpoint. Stateless costs a little per-call
session setup and removes the whole class of "the session went away" bugs.

**Tools that call the service layer directly.** Rejected: it duplicates each route's
validation, response shaping and error mapping, and creates a second definition of
"create a schema" that can silently disagree with the documented API. The ASGI hop
is 0.1 ms.

**A read-only SQL endpoint instead of an in-process tool.** Rejected: an endpoint
that accepts arbitrary SQL is a far larger surface than a tool call in the process
that already holds the connection, and the transaction-level `READ ONLY` guarantee
was worth keeping next to the validation that explains itself.

**Return the API's arrays as-is.** Rejected after testing: the SDK expands a
returned list into one content block per element, so an empty list arrives as no
content at all. Collections are wrapped as `{"count", "items"}`.
