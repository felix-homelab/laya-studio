# Architecture

## The shape of the problem

Laya does one thing: score typed questions against a text state and return
probabilities. It is fast (~33 ms), it is calibrated by construction, and it
never generates text. Everything else — storing schemas, running batches, testing
wording, gating automation on confidence, noticing when the numbers drift — is
tooling that has to sit around it.

Laya Studio is that tooling. The whole design follows from one decision:

> **An evaluation is a first-class, immutable record, and every feature points
> back at one.**

Bulk jobs, A/B tests, workflow runs, review items and calibration snapshots are
all *views onto evaluations*. That is why the audit trail is consistent no matter
which door a state came through, and why the calibration monitor can be computed
from a single source of truth.

## Layers

Requests flow strictly downward. Nothing in a lower layer imports an upper one.

```
┌─────────────────────────────────────────────────────────────────────┐
│ routers/          HTTP. Validation, status codes, serialisation.    │
│ mcp/              The same surface again, as MCP tools. No business │
│                   rules here either: tools call the routes.         │
├─────────────────────────────────────────────────────────────────────┤
│ services/         The domain. Everything that decides something.    │
│                   evaluation.py is the spine: it is the single path │
│                   every decision takes through the system.          │
├─────────────────────────────────────────────────────────────────────┤
│ inference/  llm/  Engines and generative fallbacks. Pure functions  │
│                   plus two swappable adapters. No DB access.        │
├─────────────────────────────────────────────────────────────────────┤
│ models.py  db.py  Persistence — PostgreSQL, queried via SQLAlchemy.  │
└─────────────────────────────────────────────────────────────────────┘
        ↕ observability/  wraps every request; writes the JSONL store

                    prisma/schema.prisma  ← owns the DDL
```

`observability/` is the one layer that is not in the stack, because it is a
*band* across it: a raw-ASGI middleware around every request, a logging handler on
the root logger, and process-level exception hooks. It sits outside the arrows
rather than in them so that nothing below has to know it exists — no service calls
a logger to be diagnosable, and no router imports it.


**The schema is defined outside the application.** `prisma/schema.prisma` owns the
PostgreSQL DDL; `models.py` is the query view of it. The application verifies the
schema at startup and never creates one.
`tests/test_schema_drift.py` is what keeps the two honest —
see [ADR 0012](adr/0012-prisma-owns-the-schema.md).

Two boundary rules make this hold:

* **`inference/` never touches the database.** The engine contract takes a state
  and a question bundle and returns a `DecisionResult`. Calibration *maths* lives
  there (pure); calibration *fitting* lives in `services/calibration.py` (stateful,
  DB-backed). That split is what lets the statistics be unit-tested with no
  fixtures at all.
* **`services/` never raises HTTP errors.** Services raise domain errors
  (`InvalidQuestions`, `BulkError`, `UnsafeTarget`, `ABError`, `WorkflowError`);
  routers translate them into status codes. That is why the same code paths are
  callable from the CLI, the seed script and the tests.

## The spine

`app/services/evaluation.py::run_evaluation` is the only function that turns a
state into a recorded decision. Everything else is a wrapper:

```
                    ┌──────────────────────┐
  playground ──────►│                      │
  bulk upload ─────►│                      │
  A/B test ────────►│   run_evaluation     │──► Evaluation row
  webhook / API ───►│                      │
  email ingest ────►│                      │
  workflow run ────►└──────────────────────┘
```

It performs, in order:

1. **Lint** the question bundle (`validate_questions`). Errors abort; warnings are
   surfaced but do not block.
2. **Resolve** which question drives routing — explicit request, else the first
   `choice`, else the first `noul`, else the first question.
3. **Predict** — one engine call for the whole bundle. Laya answers all questions
   in one forward pass; asking 1 vs 10 questions costs nearly the same.
4. **Normalise** the raw answer into the visualizer payload
   (`build_question_results`), tolerating the several field names Laya has used
   for the probability distribution.
5. **Calibrate** — resolve a fitted temperature per question and apply it.
6. **Persist** the whole thing: state, questions, raw answers, normalised
   results, routing, latency, engine, and the denormalised headline
   (`top_choice`, `confidence`) that the dashboards read.

Because step 6 captures the question bundle *as sent*, an evaluation can be
replayed months later against the exact taxonomy that produced it — which is what
makes A/B testing honest.

## Request paths

### A playground evaluation

```
POST /api/playground/evaluate
  → routers/playground.py        resolve schema, validate the request
  → services/evaluation.py       lint → predict → normalise → calibrate → persist
      → inference/registry.py    pick the engine (cached, thread-safe)
      → inference/laya_engine   one forward pass
      → inference/base.py        normalise into DecisionResult
      → services/calibration.py  fit/cache a temperature for this question
  → serializers.py               ORM → EvaluationResponse
```

### A webhook-triggered workflow

```
POST /api/ingest/{workflow_key}
  → deps.require_ingest_token    auth (only when a token is configured)
  → routers/ingest.py            resolve the workflow, coerce the payload
  → services/workflows.py        run_workflow
      → services/evaluation.py     score the state
      → decide_route               ladder: auto | verify | review
      → llm/service.verify_decision   only for `verify`; may degrade
      → services/actions.execute     run each matching step
      → actions.log_outcome          ActionLog row per action
  → response: route, effective_route, verification, actions, evaluation
```

Note `effective_route`: the ladder's *initial* answer and what actually happened
can differ. A `verify` item that the fallback confirms becomes `auto`; one the
fallback cannot adjudicate becomes `review`. Both facts are recorded separately so
the dashboard can show you where the safety net caught things.

### Bulk upload

```
POST /api/bulk/upload  (multipart)
  → parse CSV/JSON → rows
  → create a BulkJob, attach BulkJobItem rows
  → run_job: one run_evaluation per row, failures isolated per row
  → summarize: accuracy, ECE, band counts, prediction distribution
```

### An agent tool call

```
POST /mcp  {"method": "tools/call", "params": {"name": "evaluate", ...}}
  → observability/middleware      request id, then the response passes through
  → mcp/server.MCPEndpoint        forward to the transport built for this lifespan
      → mcp/panel.Panel           in-process ASGI call, or HTTP to another host
          → POST /api/playground/evaluate   ← the same route the UI uses
              → (everything in the first path above)
  → observability/store           two records: the tool call, and the API call it made
```

The tool does not shortcut the panel — it calls the route. That is what keeps a
tool honest about validation and error messages, and it is why an agent's own
traffic shows up in the log it can then read (`mcp_tool` names the tool that made
the call). See [ADR 0013](adr/0013-log-store-and-mcp-inside-the-panel.md).

### Any request at all

```
any request
  → observability/middleware   assign X-Request-ID, bind it to the log context
  → ...                        the log lines written below carry that id
  → record: kind=request, status, duration_ms, mcp_tool
      or kind=exception, plus the traceback
```

That is the whole point of the band: nobody below this layer had to be told. Adding
a feature does not mean adding logging; it means calling `logging.getLogger(__name__)`,
which the code already does.

Runs synchronously. See [ROADMAP.md](ROADMAP.md#the-worker-path) for why, and
what replaces it.

## The engine

One implementation: `inference/laya_engine.py::LayaEngine`, wrapping
`laya.Router`. There is no fallback and no configuration setting that selects
anything else.

That is a deliberate reversal of an earlier design where a deterministic stand-in
made the panel bootable without a multi-gigabyte download. The stand-in was
honestly labelled everywhere, but the trade was still wrong for a decision system:
a panel that can silently serve numbers no model produced teaches its operator to
trust the wrong thing. If Laya is unusable the process refuses to start and says
why. See [ADR 0009](adr/0009-laya-is-the-only-engine.md).

`DecisionEngine` (`inference/base.py`) is still an abstract protocol with three
methods — `predict`, `route`, `describe` — for two reasons: the test suite
substitutes a double through it, and a future backend has a shape to implement.
`app.inference.set_engine` is the *only* substitution point, it is test-only, and
nothing in `app/` calls it.

Nothing above `inference/` knows Laya's raw key names. `normalize_result` and the
`extract_*` helpers produce a `DecisionResult`, and that is the whole contract.
See [laya-integration.md](laya-integration.md#the-api-as-it-actually-is).

### Engine lifecycle

The engine is built on a background thread, because uvicorn binds its socket only
after the lifespan returns — building inline would make the panel refuse every
connection for the length of the download. `inference/registry.py` owns an explicit
state machine:

```
pending ──► loading ──► ready
                │
                └─────► failed
```

`get_engine()` never blocks. It returns the engine, or raises `EngineNotReady`
(temporary, carries `retry_after`) or `EngineUnavailable` (permanent). Two
exception handlers turn those into 503s; `/api/health` is liveness and `/api/ready`
is readiness. See [ADR 0011](adr/0011-background-engine-startup.md).

### Where the engine can fail

| Failure | Behaviour |
|---|---|
| `laya` not importable | `EngineUnavailable`; `status: "degraded"`, every page shows the error, decisions return 503 |
| Download fails mid-way | Same as above; the message names the cause |
| Still loading | `EngineNotReady`; decisions return 503 with `Retry-After`, pages render normally |
| Checkpoint download fails | Preload logs a warning and falls back to lazy loading; the first request pays it |
| Options exceed the token budget | `QuestionTooLarge`, surfaced as a 422 carrying the remedy |
| `head_max_len` >= `max_len` | **Refused** with a warning — applying it would break a working schema |
| GPU requested but unavailable | Laya warns and falls back to CPU itself |

## Where the interesting decisions are

Each of these is recorded as an ADR with the alternatives considered.

| Decision | Why | ADR |
|---|---|---|
| The engine contract is duck-typed around a dict, not a typed model | Laya's result schema has changed field names across versions; a tolerant normaliser beats a brittle binding | [0001](adr/0001-engine-contract.md) |
| ~~The mock engine ships in the default image~~ | **Superseded.** A panel that can serve numbers no model produced is the wrong trade for a decision system | [0002](adr/0002-mock-engine.md) → [0009](adr/0009-laya-is-the-only-engine.md) |
| PostgreSQL only, with Prisma owning the schema | Migrations had to exist, and "supports both backends" was a claim only one of them verified | [0012](adr/0012-prisma-owns-the-schema.md) |
| Unverifiable middle band escalates rather than auto-executes | A failed verification is *absence of evidence*, not evidence of correctness | [0003](adr/0003-fallback-fails-closed.md) |
| Schemas are revisioned immutably | An A/B test and a three-month-old evaluation must be replayable against the taxonomy that produced them | [0004](adr/0004-immutable-schema-revisions.md) |
| Calibration maths is separated from calibration fitting | Keeps the statistics dependency-free and unit-testable; keeps DB access out of `inference/` | [0005](adr/0005-calibration-math-vs-fitting.md) |
| Analytics aggregates in Python, not SQL | Report shape is identical on SQLite and PostgreSQL, and the metrics are testable without a database | [0006](adr/0006-analytics-in-python.md) |
| The frontend is server-rendered with no build step | The panel installs with `pip install` alone | [0007](adr/0007-no-build-step-frontend.md) |
| Ingestion is the only authenticated surface | It is the one endpoint designed to be called by third parties | [0008](adr/0008-ingest-auth-scope.md) |
| The log store is JSONL files, and the MCP server runs inside the panel | A log service is a second deployment to secure for a question a file answers; a sidecar MCP server would load the engine twice | [0013](adr/0013-log-store-and-mcp-inside-the-panel.md) |

## Extension points

| You want to… | Touch |
|---|---|
| Serve a different model | Implement `DecisionEngine`; `app.inference.set_engine` installs it. There is no config path for this by design. |
| Support another LLM vendor | Subclass `LLMProvider` in `llm/`, add it to `build_provider` |
| Add a downstream action | Add a function to `services/actions.py` and a case in `execute` |
| Add a step target | Extend `STEP_TARGETS` in `services/workflows.py` |
| Add a built-in template | Append to `TEMPLATE_DEFINITIONS` in `services/decision_schemas.py` |
| Add a linter rule | Add to `validate_questions` in `inference/base.py` |
| Add a metric | Add to `services/analytics.py`; it is plain Python over fetched rows |

## What is deliberately absent

* **No authentication on the panel.** It is an operator tool. Put it behind your
  own gateway; [deployment.md](deployment.md#hardening) covers what to check. The
  two surfaces that *can* be token-gated — ingestion and the MCP endpoint — should
  be, before the panel is reachable by anything but localhost.
* **No log shipping, and no retention beyond a window.** The JSONL store answers
  "what happened recently" and groups repeat failures; it is not an audit trail.
  Set `LAYA_STUDIO_LOG_RETENTION_DAYS` with that in mind, or turn the sink off with
  `LAYA_STUDIO_LOG_JSONL=false`. See
  [ADR 0013](adr/0013-log-store-and-mcp-inside-the-panel.md).
* **No schema migrations inside the application.** They exist, but they belong to
  Prisma and run as a separate compose service. The app only verifies.
* **No background worker.** Bulk and A/B jobs run inline; the caps
  (`MAX_BULK_ROWS`, `MAX_AB_ROWS`) keep that bounded.
* **No model training.** Laya Studio evaluates and calibrates; it does not
  fine-tune. [ROADMAP.md](ROADMAP.md#model-training-and-fine-tuning) explains why
  that is a deliberate boundary.
* **No alternative engine.** Not "none yet" — not planned. See
  [ADR 0009](adr/0009-laya-is-the-only-engine.md).
* **No streaming UI.** Laya is not a text generator, so there is nothing to
  stream.
