# Roadmap

Status of every part of Laya Studio: what is shipped, what is missing, what is
next, and what is deliberately *not* planned.

Last reviewed alongside v0.1.0, after the engine was verified against the real
runtime.

**Status legend** — `shipped` · `partial` · `not started` · `out of scope`

---

## Where the project stands

v0.1.0 is a complete vertical slice of all four modules from the brief, running
against the **real** Laya runtime. Every feature below is implemented, wired end to
end, and covered by the test suite.

Two things changed since the first draft of this roadmap, and both changed because
the runtime became reachable:

* **The mock engine is gone.** Laya is the only engine, there is no configuration
  setting that selects another, and the process refuses to start without it. See
  [ADR 0009](adr/0009-laya-is-the-only-engine.md).
* **The adapter is verified.** `tests/test_real_laya.py` runs 13 assertions against
  real checkpoints. The previous "reviewed, never executed" caveat is closed, and
  the exercise found four real bugs — documented in
  [ADR 0001](adr/0001-engine-contract.md#what-running-it-for-real-changed).
* **Startup was restructured.** The engine used to be built inside the ASGI
  lifespan, and uvicorn does not bind its socket until the lifespan returns — so a
  first `docker compose up` refused every connection for the ~42 seconds the
  checkpoints took to load, with no way to tell loading from broken. It now builds
  on a background thread: the panel answers in **0.6s**, reports its own engine
  state, and returns 503 with a `Retry-After` from anything that needs a decision.
  See [ADR 0011](adr/0011-background-engine-startup.md).
* **Storage moved to PostgreSQL, with Prisma owning the schema.** SQLite is gone
  (there is no fallback backend), `psycopg` is a required dependency, and the twelve
  tables are defined by `prisma/schema.prisma` and applied by a `migrate` compose
  service. The application verifies the schema at startup and refuses to run against
  an un-migrated database. `tests/test_schema_drift.py` migrates a scratch database
  with Prisma and fails on any difference from `app/models.py`. See
  [ADR 0012](adr/0012-prisma-owns-the-schema.md).
* **Calibration was rebuilt.** Running it on real data showed the single-temperature
  fit was not merely imprecise but structurally incapable of the correction Laya
  needs: `sigmoid(logit(p)/T)` cannot move a confidence across 0.5, and Laya's
  entropy-based confidence sits at 0.40 against 0.85 accuracy. It is now a
  two-parameter logistic map, which takes ECE from **0.446 to 0.054**. See
  [ADR 0010](adr/0010-platt-not-temperature.md).

### Part 1: Interactive decision and query playground

*Status: shipped.*

| Requirement | Status | Where |
|---|---|---|
| Form-based UI for states, questions, choices and scores | shipped | `/` — `templates/playground.html` |
| Probability visualizer: best match with exact percentage | shipped | `questionCard` in `static/js/app.js` |
| Breakdown list of alternatives and their probabilities | shipped | `build_question_results` → `breakdown`, `alternatives` |
| Act-vs-escalate indicators from confidence thresholds | shipped | `confidence_band`; plus Laya's own `act_probability` chip |
| Live schema linting while authoring | shipped | `POST /api/playground/lint`, 11 rules |
| Question cards instead of hand-written JSON | shipped | `static/js/question-editor.js` |
| Save an ad-hoc bundle as a reusable schema | shipped | "Save as new schema" in the playground |
| Bulk testing over CSV/JSON uploads | shipped | `/bulk` |
| Batch preview of model performance | shipped | job summary: accuracy, ECE, band counts, distribution |
| Schema A/B testing of wording and taxonomy | shipped | `/ab`, `services/ab.py` |

### Part 2: Workflow automation engine

*Status: shipped.*

| Requirement | Status | Where |
|---|---|---|
| Multichannel triggers: webhooks, API, email | shipped | `/api/ingest/*` — three doors |
| Conditional logic over predefined schemas | shipped | `services/workflows.py` |
| Visual step builder — blocks per ladder rung, no JSON | shipped | `/workflows`, `static/js/block-builder.js` |
| Forward data to external webhooks and APIs | shipped | `webhook` action, SSRF-guarded |
| Send automated emails | shipped | `email` action via SMTP |
| Route low-confidence items to manual review | shipped | `review` action |
| Auto-execute above 90% | shipped | `route == "auto"` |
| 60–90% → secondary verification or generative LLM | shipped | `route == "verify"` → `llm_verify` |
| Full audit of every action | shipped | `action_logs`, visible in the run inspector |

### Part 3: Custom schema and template management

*Status: shipped.*

| Requirement | Status | Where |
|---|---|---|
| No-code schema editor: create, save, reuse | shipped | `/schemas` — one card per question, live lint |
| Primitive selector: choice, score, noul | shipped | a dropdown per question; criteria follow the primitive |
| Built-in templates: triage, fraud, moderation | shipped | four seeded templates |
| Versioning and reuse across workflows | shipped | immutable `schema_revisions`, fork-to-edit |

### Part 4: Real-time analytics and confidence dashboard

*Status: shipped.*

| Requirement | Status | Where |
|---|---|---|
| Confidence distribution | shipped | `/analytics` histogram, band-coloured |
| Certainty and escalation trends over time | shipped | stacked volume chart, `escalation_rate` |
| Calibration monitor (ECE) | shipped | reliability diagram, ECE/MCE/Brier, daily trend |
| Manual review queue workspace | shipped | `/review` — inspect, approve, correct, reject |
| Automated action execution | shipped | `noop`, `webhook`, `email`, `review` sinks |
| Escalation rates | shipped | per-band rates, per-route mix, correction rate |

### Deliverables

*Status: shipped.*

| Deliverable | Where |
|---|---|
| Complete directory structure and database models | 12 tables, documented in [data-model.md](data-model.md) |
| Inference service plus hybrid LLM fallback wrappers | `inference/`, `llm/` — OpenAI and Anthropic |
| Frontend UI components | 7 pages, server-rendered with vanilla JS |
| `Dockerfile` and `docker-compose.yml`, single command | one image, two compose profiles |

---

## Known limitations

Ordered by how likely each is to bite you.

### 1. Bulk and A/B jobs run synchronously

*Status: partial.*

A 5,000-row upload holds the HTTP request open for the whole job. At real Laya
latency — measured ~17 ms per question per row on MPS — a 5,000-row job against a
four-question schema is on the order of ten minutes. Failures are isolated per row
and the caps (`MAX_BULK_ROWS`, `MAX_AB_ROWS`) keep it bounded, but a proxy timeout
will kill a large job halfway.

This is the first thing to fix for production use.
See [The worker path](#the-worker-path).

### 2. The confidence ladder's defaults are not calibrated for anyone

*Status: partial.*

`0.90` / `0.60` come from the brief. They are not derived from measurement, and
measurement says they are wrong for the default templates: Laya's `confidence` is
an entropy measure, so a 4-way `choice` rarely exceeds 0.9 even with an
overwhelming winner. On the bundled 40-row sample, **no row reached the auto band**.

The system handles this correctly — the monitor measures ECE, refitting sharpens or
softens, and thresholds can be set per workflow. But the *defaults* still invite the
mistake of believing a 0.90 threshold means 90% precision on a question it was never
tuned for.

**Next step:** derive per-schema suggested thresholds from the observed confidence
distribution and ECE, and surface them on the calibration panel rather than leaving
the operator to do the arithmetic.

Note that calibration changes this materially: on the bundled sample, rows reaching
a 0.90 auto threshold went from 2 to 15 once the map was fitted. So the honest
sequencing is *load labelled data → refit → then set thresholds*, and the panel
should say so rather than letting someone tune against raw entropy confidences.

### 3. Calibration is top-1 and per-question

*Status: partial.*

The monitor measures whether the *winning* label's confidence is honest, and fits
one map per `(schema, question)`.

Two consequences: the runner-up probabilities in a `choice` breakdown are not
verified; and a model over-confident on one specific label cannot be corrected,
because the map is shared across every label of the question.

A third, milder one: the two-parameter map needs more labelled rows than a single
temperature did to fit *stably*. Below roughly 30 the ridge dominates and the map
stays near the identity — conservative and correct, but it means very small samples
get little correction.

### 4. Analytics aggregates in Python

*Status: partial.*

`services/analytics.py` fetches rows and groups them in Python rather than pushing
the work into SQL. Deliberate ([ADR 0006](adr/0006-analytics-in-python.md)) — it
keeps SQLite and PostgreSQL identical and makes the metrics testable with no
database — but it caps at `MAX_ANALYTICS_ROWS` (50,000) per window and gets slow
before that.

### 5. The schema is defined twice

*Status: partial.*

`prisma/schema.prisma` owns the DDL and `app/models.py` mirrors it for querying.
That is two definitions of one schema, and they will drift unless something checks —
which is why `tests/test_schema_drift.py` exists and why it compares every column,
constraint and index name rather than just the table list.

It catches drift; it does not prevent it. A schema change is genuinely two edits.

**Next step:** the drift test can generate the second edit. `prisma migrate diff`
emits SQL, and the reverse — SQLAlchemy metadata from a live schema — is a short
generator script. A `make db-sync-models` that rewrites `models.py` from the
migrated database would make Prisma the *only* thing anyone edits.

### 6. No authentication on the panel

*Status: partial.*

Only `/api/ingest/*` is authenticated. Every other endpoint — including schema
editing and workflow definitions — is open. Intentional for an operator tool, and
[deployment.md](deployment.md#hardening) says to put it behind your own access
control, but it means the panel must never be exposed directly.

### 7. Install is heavy

*Status: partial.*

torch plus ~800 MB of weights before the first evaluation, and a ~2.5 GB image.
This is the direct cost of [ADR 0009](adr/0009-laya-is-the-only-engine.md) and it
is accepted, not mitigated. What could still be better:

* the Dockerfile installs CPU torch and then `laya`, which pulls `transformers`
  again — the layers could be ordered and pinned more tightly;
* there is no multi-stage build trimming build-time tooling;
* no ARM/CUDA variant matrices are published.

### 8. No retry, signing or dead-letter handling on webhooks

*Status: partial.*

A webhook action fires once. A non-2xx is logged as `failed` and nothing else
happens. No backoff, no retry queue, and no HMAC signature on the outgoing payload
so the receiver can verify it came from you.

### 9. Tailwind is loaded from a CDN

*Status: partial.*

Keeps the frontend build-step-free ([ADR 0007](adr/0007-no-build-step-frontend.md)),
at the cost of an external render-blocking request and a manual vendoring step for
air-gapped deployments.

### 10. Other gaps

| Gap | Impact |
|---|---|
| No CI configuration | `make check` is the whole pipeline; nothing runs it automatically. |
| No Prometheus metrics endpoint | Monitoring scrapes `/api/health` and `/api/ready`. Engine startup duration is only in the logs. |
| No data retention or pruning | `evaluations` grows without bound. [data-model.md](data-model.md#retention) has manual queries. |
| No `predict_shortlist` support | Laya ships it; it is the right answer above ~11 options. See [priority 2](#9-predict_shortlist-for-high-cardinality-choice). |
| No load test | Inference is deliberately unserialised, on the strength of Laya's own design note. Unmeasured. |
| Email ingestion is a webhook shape | There is no IMAP poller. Point a mail worker at `/api/ingest/email/{key}`. |
| No rate limiting | An open ingestion endpoint can be flooded. Laya inference is now the expensive resource behind it. |
| Review queue has no bulk actions | Corrections are one item at a time. |
| No export | No CSV/JSON export of evaluations, reviews or calibration snapshots. |
| No per-user attribution | `assignee` is a free-text string, not an identity. |
| No i18n | The UI is English-only. (`laya-multilingual` handles *input* language, which is a different thing.) |

---

## Next

Prioritised. Each item says what it unblocks, so you can reorder by your own needs.

### Priority 0: makes it trustworthy

#### 1. Configure the ladder from measurement, not from a default

*Status: not started.*

Limitation #2, and the highest-value item left. The system already computes
everything needed — ECE, the reliability diagram, the confidence histogram, band
counts — it just does not close the loop back into the thresholds.

Concretely: after a bulk run or a calibration refit, propose thresholds that
achieve a target precision on the labelled rows, and show the resulting
`auto`/`verify`/`review` split before applying them. A "suggested thresholds"
panel on the calibration card, with an apply button per workflow.

The arithmetic is a sweep over the labelled `(confidence, correct)` pairs: pick the
lowest threshold whose observed precision at or above it meets the target. The
refit endpoint already has the data in hand.

#### 2. Move bulk and A/B onto a worker

*Status: not started.*

Limitation #1. A job queue (RQ, arq, or `BackgroundTasks` with polling) driven by
the existing `BulkJob.status` field. The schema already supports it — `pending`,
`running`, `succeeded`, with progress counters — because the synchronous
implementation writes those same fields.

See [The worker path](#the-worker-path) for the full scope.

#### 3. Load-test concurrent inference

*Status: not started.*

`LayaEngine.predict` is deliberately unserialised, on the strength of Laya's
`Router` docstring stating that `Agent.system_one` is left outside its lock so
concurrent predictions share a checkpoint. That is a reasonable reading of the
design and it is untested.

The test is cheap now: run N concurrent requests through `TestClient` against a
real checkpoint and assert no corruption and sensible throughput scaling. If it
turns out inference is *not* safe to share, the fix is a semaphore around
`Agent.system_one` and a documented concurrency ceiling — which is important to
know before a production deployment discovers it.

### Priority 1: makes it operable

#### 4. Generate `models.py` from the migrations

*Status: not started.*

Limitation #5. `make db-sync-models` would introspect the migrated database and emit
the SQLAlchemy models, making `prisma/schema.prisma` the only schema anyone edits by
hand. The drift test already builds the comparison; this is the inverse.

Until then: change `schema.prisma`, run `make db-migrate-dev`, update `models.py`,
and let the drift test confirm you got it right.

#### 5. Prometheus metrics

*Status: not started.*

`/metrics` with the counters that matter: evaluations by source and checkpoint,
workflow runs by route, action outcomes, review queue depth, ECE per active slice,
inference latency histogram, questions per call, and — newly relevant — engine
startup duration and a gauge for whether the engine is ready. The analytics layer
computes the rest already; this is packaging.

#### 6. Webhook delivery hardening

*Status: not started.*

HMAC-SHA256 signing (`X-Laya-Signature`), exponential backoff with a bounded retry
count, and a dead-letter list surfaced on the workflows page. `ActionLog` will need
an `attempt` column and a `next_retry_at`.

#### 7. Image size and layer hygiene

*Status: not started.*

Limitation #7. Pin `transformers` explicitly instead of letting `laya` pull a
version after torch is installed, use a build stage for anything not needed at
runtime, and document the CUDA/CPU build matrix. Target: a runtime layer under
1 GB excluding torch.

#### 8. Data retention

*Status: not started.*

`laya-studio prune --older-than 90d --source api`, plus optional scheduled
execution. Must never prune a row that has a `label` — that is the calibration
history — and should warn loudly about how many that leaves.

PostgreSQL makes this easier than SQLite did: `DELETE ... RETURNING` for a dry-run
count, and partitioning by `created_at` is available if the table gets large enough
to need it.

### Priority 2: makes it better

#### 9. predict_shortlist for high-cardinality choice

*Status: not started.*

Laya ships `predict_shortlist(agent, state, questions, embed_fn, k)`, which ranks
option labels by embedding similarity and scores only the top `k`. It is the right
answer above ~11 options, where the `choice:11+` temperature bucket is uncalibrated
*and* the token budget starts truncating.

`laya.common.embed_fn_from_agent` provides the embedding function from a loaded
agent, so this is wiring rather than research. Needs: a schema-level opt-in, a
`k` setting, surfacing the `shortlist` metadata Laya returns (which labels were
dropped and with what scores), and a linter hint pointing at it.

Until this lands, the guidance is: keep `choice` taxonomies under 11 options.

#### 10. Per-label calibration

*Status: not started.*

Extend `CalibrationSnapshot` with a `label` column and fit one temperature per
`(question, label)` where the sample allows, falling back to the question-level fit.
Addresses the second half of limitation #3. `report_for` already filters by schema
and question; adding a label filter is mechanical.

#### 11. CI

*Status: not started.*

`make check` on push across Python 3.10 / 3.12 / 3.13, plus a second job with a
Hugging Face cache running `pytest --real-laya`. Neither needs a GPU or a secret.

#### 12. Review queue ergonomics

*Status: not started.*

Bulk resolve, keyboard shortcuts, and a "next item" flow. Functional one-at-a-time
today, which is fine for 20 items and painful for 500 — and the bundled sample
alone queues 31.

#### 13. Export

*Status: not started.*

CSV/JSON export of evaluations, review items and calibration snapshots for offline
analysis. `Accept: text/csv` content negotiation on the existing list endpoints.

#### 14. Idempotency keys for manual runs

*Status: not started.*

`POST /api/workflows/{id}/run` is not idempotent. An optional `Idempotency-Key`
header so a retrying caller does not create duplicate runs.

### Priority 3: speculative

Genuinely uncertain value. Listed so the ideas are not lost.

* **Cost accounting.** Track tokens per workflow by joining `llm_verify` action logs
  against a price table. Answers "what is the hybrid fallback costing me?".
* **Active learning.** Prioritise review items by expected information gain
  (highest uncertainty, highest impact) rather than raw priority. The data is there.
* **Rule-based pre-filters.** Skip Laya for states matching an obvious regex — a
  real saving now that inference is the expensive step.
* **Schema test cases.** Attach expected answers to a schema so a revision change
  runs its own regression suite on save.
* **Checkpoint A/B.** Compare `english` against `typed-decisions` on one dataset —
  the A/B machinery already takes a dataset and a schema; it would need a
  checkpoint dimension added.

---

## Deliberately out of scope

Not "not yet" — **not planned**, with reasoning.

### Model training and fine-tuning

Laya Studio evaluates and calibrates Laya. It does **not** train it.

Fine-tuning is where the value is — the base checkpoints are near-chance zero-shot
— but it is a substantially different product: data curation, training loops,
evaluation harnesses, GPU orchestration, a model registry, versioned weights.
Half-building it would be worse than not starting.

The boundary is clean: Laya Studio produces the **labels** a fine-tuning pipeline
needs, via review corrections and labelled uploads, and consumes the **served**
result. Export the labels, train elsewhere, point `LAYA_STUDIO_LAYA_CACHE_DIR` at
the output.

### An alternative or fallback engine

Not "none today" — none ever. See
[ADR 0009](adr/0009-laya-is-the-only-engine.md). A decision system whose output can
be numbers no model produced teaches its operator to distrust the wrong thing.

The `DecisionEngine` protocol stays so the test suite can substitute a double, not
so a configuration value can.

### Multi-tenancy and user accounts

A single-operator tool. Accounts, teams, RBAC and tenant isolation are an
application platform, not a panel. Put it behind your identity provider.
`ReviewItem.assignee` is a free-text string precisely because real identity belongs
to a layer above this one.

### A React or Vite single-page app

The frontend is server-rendered with vanilla JS on purpose: it installs with
`pip install` alone and there is no build step to keep working. This would change
if the UI grew a real-time collaborative editor or a virtualised 100k-row table.
Neither is planned.

### Streaming responses

Laya is not a generative model. `usage.output_tokens` is always `0`. There is
nothing to stream.

### A general-purpose LLM gateway

There are already good ones — LiteLLM, OpenRouter, vLLM. Laya Studio implements
exactly two providers for exactly one purpose: adjudicating an uncertain decision
with as few tokens as possible. Point `LAYA_STUDIO_OPENAI_BASE_URL` at a gateway if
you need breadth.

### Vector search over evaluations

Tempting ("find similar past decisions"), but it belongs to a different product and
would need an embedding model the panel does not have. Note that this is distinct
from [shortlisting](#9-predict_shortlist-for-high-cardinality-choice), which uses an
embedding the *loaded checkpoint already provides*.

---

## The worker path

The largest planned change, spelled out because it touches several layers.

**Today:**

```
POST /api/bulk/upload → parse → create job → attach rows → run all rows → return
                                                    ^
                                        request held open for the whole job
```

**Target:**

```
POST /api/bulk/upload ──► parse → create job (status=pending) → enqueue → 202
                                        │
                                  worker picks up
                                        │
                                  status=running, processed_rows++ per row
                                        │
                                  status=succeeded|failed
                                        │
GET /api/bulk/jobs/{id} (poll)  ◄───────┘
    or GET /api/bulk/jobs/{id}/events (SSE)
```

What already supports this:

* `BulkJob.status` has `pending`, `running`, `succeeded`, `failed`.
* `processed_rows` and `failed_rows` are already incremented per row.
* `started_at` and `finished_at` are already written.
* `BulkJobItem` rows are persisted before the run, so a worker can resume.
* `run_job(db, job)` takes a job and does the work; it does not need the request.

What needs to change:

1. `run_job` must be callable without the request's session — it mostly already is.
2. `POST /api/bulk/upload` returns `202` with the job instead of `200` with results.
   **This is a breaking API change**, worth doing before anyone depends on it.
3. A worker entry point (`python -m app.worker`) and a queue.
4. The `/bulk` page polls instead of reading the response body.

Same shape for A/B tests, which already carry `ABTest.status`.

The load model that makes this urgent: one Laya forward pass serves all questions
in a bundle, measured at roughly 17 ms per question per row on MPS. A 5,000-row job
with a four-question schema is ~10 minutes of inference that currently blocks a
request thread.

---

## How to pick up work

1. Read [architecture.md](architecture.md) for the layering rules.
2. Read [laya-integration.md](laya-integration.md) before changing anything in
   `inference/`. Almost every surprising behaviour there was found by running the
   runtime, not by reading about it.
3. Read the relevant ADR in [adr/](adr/) before changing a structural decision. If
   you overturn one, add a new ADR saying so rather than editing the old one —
   [0002](adr/0002-mock-engine.md) is kept as a superseded record for exactly this
   reason.
4. `make test` must pass. Anything structural about Laya belongs in
   `tests/test_real_laya.py`; see [testing.md](testing.md#adding-to-the-real-runtime-suite).
5. Keep the honest labelling. If a change makes a confidence look more certain than
   the model reported, or makes a failed verification look like a successful one,
   that is a regression regardless of what the tests say.

### A note on the honesty invariants

Four properties exist to stop this system from lying to its operator:

1. **There is no engine that is not Laya.** No fallback, no simulation, no
   configuration that selects one.
2. **A failed fallback escalates.** It never auto-executes on an unverified middle
   band.
3. **A temperature is only fitted when the sample supports one**, and the identity
   is returned with an explanation otherwise.
4. **The reliability diagram shows stated confidence and observed accuracy side by
   side**, so miscalibration is visible rather than inferred — and the checkpoint's
   own temperature table is on the same page, so you can see what Laya already did
   before Laya Studio touched anything.

Any change that weakens one of these needs a very good reason and an ADR.
