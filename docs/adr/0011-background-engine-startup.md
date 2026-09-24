# 0011 — The engine is built in the background; the panel serves immediately

**Status:** Accepted

## Context

The engine used to be constructed inside the ASGI lifespan:

```python
@asynccontextmanager
async def lifespan(app):
    ...
    require_engine()      # blocks: may download ~800 MB and load 1.16B parameters
    yield
```

Measured on an M-series laptop with a warm cache: **42 seconds**. On a cold cache,
where the checkpoints download first, several minutes.

The assumption was that this is acceptable because the process "refuses to start
rather than serving simulated numbers" — a deliberate property from
[ADR 0009](0009-laya-is-the-only-engine.md).

It is not acceptable, and the reason is a uvicorn detail rather than a Laya one:

> **uvicorn binds its listening socket only after the lifespan startup returns.**

Verified directly — a lifespan that sleeps for 20 seconds, polled every 3s:

```
t= 3s  not accepting: ConnectionRefusedError
t= 6s  not accepting: ConnectionRefusedError
...
t=18s  not accepting: ConnectionRefusedError
LIFESPAN: done
```

So during the entire download, `docker compose up` produces a container that is
mapped to a host port and **refuses every connection**. Not a slow page, not an
error page, not a "loading" spinner — `ECONNREFUSED`. An operator sees the port
mapped, the container running, and nothing answering, with no way to tell whether
it is loading, broken, or misconfigured.

The healthcheck made it worse. It is a *liveness* probe, and Docker reports
`starting` for the whole `start_period`, so there was no signal either.

## Decision

Construct the engine on a background thread and serve from the first instant.

1. **`inference/registry.py` owns the lifecycle** as an explicit state machine:
   `pending → loading → ready | failed`.
2. **`start_engine(background=True)`** is called from the lifespan and returns
   immediately. `background=False` blocks, which is what the CLI wants.
3. **`get_engine()` never blocks.** It returns the engine when ready, raises
   `EngineNotReady` (temporary, carries `retry_after`) while loading, and
   `EngineUnavailable` (permanent) when the build failed.
4. **Two exception handlers in `main.py`** turn those into `503` JSON responses
   with a `Retry-After` header. A broken runtime is a 503 explaining itself, not an
   opaque 500.
5. **Pages never consult the engine in a way that can raise.** `page_context` uses
   `engine_status()`, a pass-through that cannot fail, so every page renders during
   loading and shows a banner saying so.
6. **`/api/health` is liveness; `/api/ready` is readiness.** Health returns 200 as
   soon as the process serves — including while loading — and reports
   `status: "starting" | "ok" | "degraded"`. `/api/ready` returns 503 until the
   engine is usable, and is what an orchestrator should gate traffic on.

The Docker healthcheck stays on `/api/health` with a 15-second `start_period`; the
readiness alternative is documented in the Dockerfile and compose file.

## Consequences

Measured against the real runtime, `uvicorn app.main:app`, warm cache:

| | before | after |
|---|---|---|
| Port accepts connections | 42.1 s | **0.6 s** |
| `GET /` | refused | **200**, with a "Laya is still loading" banner |
| `GET /api/health` | refused | **200**, `status: "starting"` |
| `GET /api/ready` | refused | **503**, `Retry-After: 10` |
| `POST /api/playground/evaluate` | refused | **503**, `Retry-After: 10` |
| Engine ready | 42.1 s | 42.1 s |
| `POST /api/playground/evaluate` | 200 | **200**, `billing @ 0.947` |

**Good.**

* The panel is diagnosable from the first second, which is the whole point. A
  failure to load is now *visible* rather than indistinguishable from a hung
  container.
* The engine build did not get slower; the same 42 seconds still elapse. They are
  just no longer spent refusing connections.
* A permanent failure is legible: `status: "degraded"`, the exception message on
  `/api/health`, the full traceback on every page, and a 503 on any decision.
* `EngineUnavailable` and `EngineNotReady` are now distinct types, so callers can
  tell "come back in a minute" from "this will never work". The old code conflated
  them, and a broken runtime surfaced as an unhandled `RuntimeError` → 500.

**Bad.**

* **Two new failure modes to reason about.** An endpoint can now be reachable but
  not usable. Anything integrating over HTTP must handle a 503 with `Retry-After`
  rather than assuming a successful connection means a successful call.
* **Readiness is now the caller's problem.** Deployments must use `/api/ready` for
  routing, or accept a window where a replica 503s. Documented, but a thing to get
  right.
* **A partially-initialised process exists.** Between `start_engine` and `ready`
  the app is serving with no engine. Nothing in the request path assumes otherwise,
  but it is a state that did not exist before.
* Reading the code is slightly harder: the engine is no longer available where the
  lifespan "finished", so an endpoint's engine access is a call that can raise
  rather than a captured reference.

**Neutral.**

* The state machine is process-local. Multiple replicas each load their own copy,
  which was already true.

## Alternatives considered

**Keep blocking, but bind the socket first.** Would require bypassing uvicorn's
server lifecycle, or running a second proxy process. Rejected: using a framework
against its grain to preserve a startup ordering we do not actually need.

**Lazy construction on first use.** The server would bind instantly and the first
*decision* would pay the download. Rejected because it moves a multi-minute stall
onto whatever unlucky request arrives first, with a client timeout in the middle —
strictly worse than a clean 503.

**Keep the hard failure but make the container fail fast and loudly.** This is what
the code did, and the user-visible symptom was still `ECONNREFUSED` with a restart
loop. Rejected: it makes the failure noisy in the logs and invisible on the wire.

**Compile to an image with the weights baked in.** Removes the download, not the
load, so ~20 s of the 42 remains — and it was already rejected in
[ADR 0009](0009-laya-is-the-only-engine.md) for image-size reasons.

**Serve a static "starting" page from a proxy until ready.** Achieves the same
visibility with an extra moving part, and the panel could not have reported *why*
it was waiting. Rejected: the application already has the information.
