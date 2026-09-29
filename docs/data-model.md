# Data model

The schema is organised around one idea: **an evaluation is first-class**.
Everything else points at one, which is what keeps the audit trail consistent and
makes the analytics computable from a single source of truth.

```
                    EvalSchema ──1:N──► SchemaRevision
                        │                    │
                        │                    │ (pinned by)
                        ▼                    ▼
   BulkJob ──► BulkJobItem ──────────► Evaluation ◄────────── ABTestItem
                                          ▲
                                          │
                    Workflow ──► WorkflowRun ──► ActionLog
                        │             │
                        └─────────────┴────► ReviewItem ──► (label written back
                                                              onto Evaluation)

                    CalibrationSnapshot  (fitted from labelled Evaluations)
```

### PostgreSQL, and Prisma owns the DDL

The database is **PostgreSQL only**, and the schema is defined by
[`prisma/schema.prisma`](../prisma/schema.prisma) — the `prisma migrate` CLI creates
and evolves it. The SQLAlchemy models in `app/models.py` are a **query layer**, kept
in step with the migrations by `tests/test_schema_drift.py`, which migrates a
scratch database with Prisma and fails on any difference.

That means a schema change is two edits: `prisma/schema.prisma` (then
`make db-migrate-dev`), and `app/models.py`. Forget the second and the drift test
tells you; forget the first and the migration is empty.

| | |
|---|---|
| Primary keys | `integer` with a sequence (Prisma `Int @id @default(autoincrement())`) |
| Timestamps | `timestamptz(3)`, always UTC |
| JSON columns | `jsonb` — indexable, and what Prisma's `Json` maps to |
| Text columns | `text`. `varchar(n)` is not used: PostgreSQL gives it no advantage, and length limits are checked in the application |
| Foreign keys | Every FK column is indexed — PostgreSQL does not do it for you, and without one each `ON DELETE SET NULL` / `CASCADE` scans the child table. A composite or unique index that leads with the column counts; no single-column duplicate is added next to it |

`created_at` and `updated_at` carry `DEFAULT CURRENT_TIMESTAMP` at the database
level as a safety net, and are also set by the ORM. `updated_at`'s automatic bump
on UPDATE is the ORM's `onupdate`, not a database trigger.

---

## Part 3 — schema & template management

### `eval_schemas`

A reusable, named bundle of typed questions.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `key` | str, unique, indexed | URL-safe slug, auto-deduplicated (`repeat`, `repeat-2`) |
| `name` | str(200) | |
| `description` | text | |
| `primitive` | str(16) | `choice` \| `score` \| `noul` \| `mixed` — derived from the questions |
| `definition` | JSON | `{"questions": {...}}` — the exact dict handed to `Router.predict` |
| `tags` | JSON list | free-form |
| `is_template` | bool | built-ins cannot be deleted, only forked |
| `current_revision` | int | pointer to the latest revision number |
| `created_at`, `updated_at` | datetime | |

Relationships: `revisions` (cascade delete), `evaluations`.

### `schema_revisions`

Immutable snapshots. A revision is minted whenever `definition` actually changes
— metadata-only edits do not bump it.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `schema_id` | FK → `eval_schemas`, cascade | |
| `revision` | int | unique per schema (`uq_schema_revision`) |
| `definition` | JSON | frozen question bundle |
| `note` | text | why this revision exists |
| `created_at` | datetime | |

**Why this table exists.** A/B tests compare two revisions; a three-month-old
evaluation must be replayable against the taxonomy that produced it. Mutating a
schema in place would make both impossible — you would be comparing a revision
against itself and never know. See [ADR 0004](adr/0004-immutable-schema-revisions.md).

Once a revision is written it is never edited. Deleting a schema removes its
revisions (they are meaningless without it), but evaluations keep their own copy
of the question bundle in `evaluations.questions`.

---

## Part 1 — playground, bulk and A/B

### `evaluations`

One forward pass of Laya over one state, with everything it returned.

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `schema_id` | FK → `eval_schemas`, `SET NULL` | nulled, not deleted, if the schema goes |
| `schema_revision_id` | FK → `schema_revisions`, `SET NULL` | |
| `state` | JSON | the dict handed to Laya |
| `state_text` | text | flattened rendering, for display and search |
| `questions` | JSON | **the bundle as sent** — this is what makes replay possible |
| `answers` | JSON | raw `result["answers"]`, verbatim |
| `question_results` | JSON list | the normalised visualizer payload |
| `routing` | JSON | `result["routing"]` — checkpoint plus reason |
| `engine` | str(32) | `laya`. Recorded per row because it is the first thing you want to know about a historical decision. |
| `engine_model` | str(64) | Which checkpoint answered: `english` \| `multilingual` \| `typed-decisions`. Read from `routing["model"]` — the top-level `model` key is Laya's internal agent class name. |
| `primary_question` | str(120) | which question drives routing |
| `top_choice` | str(200), indexed | denormalised headline |
| `confidence` | float, indexed | **after** calibration |
| `raw_confidence` | float | before calibration, kept for comparison |
| `calibrated` | bool | whether a temperature was applied |
| `latency_ms` | float | measured, not estimated |
| `source` | str(24), indexed | `playground` \| `api` \| `webhook` \| `email` \| `bulk` \| `workflow` \| `ab_test` |
| `label` | str(200) | ground truth, when known |
| `label_source` | str(24) | `human` \| `dataset` \| `review` |
| `created_at`, `updated_at` | datetime | |

Indexes: `(created_at, source)`, plus `confidence` and `top_choice` for dashboards.

**Storing both `confidence` and `raw_confidence`** is what lets the analytics page
answer "would this decision have been different before the refit?" without
re-running anything. Note what `confidence` is: Laya reports a normalised Shannon
entropy, `1 - H(p)/log(k)`, not the probability of the winning label. See
[laya-integration.md](laya-integration.md#four-details-that-are-easy-to-get-wrong).

**`label` is the currency of the whole calibration loop.** It is written by:
the playground's ground-truth box, a bulk upload's label column, an A/B dataset,
or a review-queue resolution. Without it, ECE is uncomputable.

### `bulk_jobs` and `bulk_job_items`

A CSV/JSON upload evaluated row by row.

`bulk_jobs`:

| Column | Notes |
|---|---|
| `name`, `filename` | |
| `schema_id` | FK, `SET NULL` |
| `status` | `pending` \| `running` \| `succeeded` \| `failed` |
| `total_rows`, `processed_rows`, `failed_rows` | progress counters |
| `options` | JSON: `state_column`, `label_column`, `model`, `columns`, `format`, parse warnings |
| `summary` | JSON: accuracy, ECE, MCE, Brier, band counts, prediction distribution |
| `error` | text |
| `started_at`, `finished_at` | |

`bulk_job_items`: one row per input row — `row_index`, `state`, `label`,
`predicted`, `confidence`, `ok`, `error`, and a nullable `evaluation_id`.
`(job_id, row_index)` is unique (`uq_bulk_job_item_row`), so a row cannot be
counted twice. Per-row failure isolation is deliberate: one malformed row must not fail a
2000-row job.

### `ab_tests` and `ab_test_items`

`ab_tests`: `name`, `schema_id`, `revision_a_id`, `revision_b_id`, `status`,
`dataset` (JSON, the frozen input rows), `summary` (JSON), `finished_at`.

`ab_test_items`: per row, `choice_a`/`choice_b`, `confidence_a`/`confidence_b`,
`flipped` (bool), and the full `result_a`/`result_b` payloads. `(test_id, row_index)`
is unique (`uq_ab_test_item_row`); a re-run deletes the old items first.

`summary` carries `flip_rate`, `agreement`, `mean_confidence_delta`,
`transitions` (only the rows that moved), and when labels exist:
`accuracy_a`, `accuracy_b`, `accuracy_delta`, `fixed_by_b`, `broken_by_b`,
`net_gain`, plus a plain-English `interpretation`.

`fixed_by_b`/`broken_by_b` are what separate "accuracy went up" from "accuracy
went up for a reason". See [analytics-and-calibration.md](analytics-and-calibration.md#reading-an-ab-test).

---

## Part 2 — workflow automation

### `workflows`

A trigger, a schema, a confidence ladder, and ordered steps.

| Column | Notes |
|---|---|
| `key` | unique slug — this is what `/api/ingest/{key}` uses |
| `enabled` | disabled workflows refuse to run rather than erroring |
| `trigger_type` | `api` \| `webhook` \| `email` \| `manual` |
| `trigger_config` | JSON — reserved for per-trigger options |
| `schema_id` | FK, `SET NULL` |
| `decision_question` | which question drives routing; empty = auto-pick |
| `steps` | JSON list of `{on, kind, config}` |
| `auto_threshold`, `verify_threshold` | per-workflow ladder, defaulting to the global settings |

**Invariant:** `verify_threshold <= auto_threshold` is enforced on create *and*
update. A verify threshold above the auto threshold would make the middle band
unreachable.

`steps[].on` ∈ `auto` \| `verify` \| `review` \| `always`.
`steps[].kind` ∈ `webhook` \| `email` \| `review` \| `noop`.

### `workflow_runs`

| Column | Notes |
|---|---|
| `workflow_id` | FK, cascade |
| `evaluation_id` | FK, `SET NULL` |
| `status` | `pending` \| `running` \| `succeeded` \| `failed` |
| `route` | what the ladder decided initially |
| `confidence` | the confidence the decision rested on |
| `final_choice` | after any verification — may differ from the model's |
| `actions` | JSON list of outcome summaries |
| `trigger_payload` | JSON — the inbound payload, for auditing |
| `error`, `started_at`, `finished_at` | |

`route` records the *initial* decision; the response's `effective_route` records
what actually happened. Both are kept, so "the fallback caught this" is
distinguishable from "the ladder sent it to a human directly".

### `action_logs`

Every action, persisted.

`kind` (`webhook` \| `email` \| `review` \| `llm_verify` \| `noop`), `target`,
`status` (`ok` \| `failed` \| `skipped`), `request`, `response`, `error`,
`latency_ms`, `created_at`.

`llm_verify` is logged like any other action, so the generative fallback appears
in the same visualizer as the webhooks it feeds.

---

## Part 4 — analytics, calibration and review

### `calibration_snapshots`

A fitted temperature plus its supporting statistics, for one slice.

| Column | Notes |
|---|---|
| `primitive` | `choice` \| `score` \| `noul` |
| `question` | which question; empty string = schema-wide |
| `schema_id` | FK, `SET NULL`, indexed |
| `n_samples` | how many labelled rows the fit used |
| `temperature` | the fitted T |
| `ece`, `mce`, `brier` | **post-fit** quality metrics |
| `accuracy`, `mean_confidence` | the two numbers whose gap *is* the miscalibration |
| `bins` | the reliability diagram, serialised |
| `created_at` | indexed; the API returns the latest per slice |

Normalisation is per `(schema, question)`, because over-confidence differs by
question type and option count — the reason Laya's own model card recommends
refitting per surface rather than once globally.

Rows below `LAYA_STUDIO_CALIBRATION_MIN_SAMPLES` (default 30) are never fitted;
the evaluator falls back to identity.

### `review_items`

| Column | Notes |
|---|---|
| `workflow_id`, `workflow_run_id`, `evaluation_id` | all nullable, all `SET NULL` |
| `state`, `state_text` | copied so the item is readable if the evaluation goes |
| `question`, `model_choice`, `model_confidence` | what the model said |
| `alternatives` | JSON — the ranked runner-up options, for the reviewer |
| `reason` | `low_confidence` \| `verification_disagreement` \| `verification_unavailable` \| `moderation_uncertain` \| custom |
| `priority` | int, descending sort |
| `status` | `pending` \| `in_review` \| `approved` \| `corrected` \| `rejected` |
| `assignee`, `corrected_label`, `notes` | |
| `created_at`, `resolved_at` | |

**De-duplication:** `enqueue` will not create a second open item for the same
`evaluation_id`. Re-running a workflow on an item already in the queue updates
nothing and returns the existing row.

**The important behaviour** is in `resolve`:

| Resolution | Label written to the evaluation |
|---|---|
| `approved` | the model's own choice |
| `corrected` | the human's `corrected_label` (required — 422 without it) |
| `rejected` | *nothing*; the row is marked not-a-decision and excluded from calibration |

That write-back is why review is a learning mechanism and not just a worklist.

---

## Query notes

### Finding miscalibration candidates

```sql
-- Rows where the model was confident and wrong: prime candidates for review.
SELECT id, primary_question, top_choice, confidence, label
FROM evaluations
WHERE label IS NOT NULL
  AND confidence >= 0.9
  AND lower(top_choice) <> lower(label)
ORDER BY confidence DESC
LIMIT 50;
```

`jsonb` columns are queryable directly, which the SQLite build could not do:

```sql
-- Which checkpoint answered, straight out of the routing payload.
SELECT routing->>'model' AS checkpoint, count(*)
FROM evaluations
GROUP BY 1 ORDER BY 2 DESC;
```

### Confirming a calibration refit did what you expect

```sql
-- Raw vs calibrated confidence, side by side.
SELECT id, raw_confidence, confidence, calibrated, label, top_choice
FROM evaluations
WHERE calibrated = 1
LIMIT 20;
```

### Calibration slice coverage

```sql
SELECT schema_id, primary_question, COUNT(*) AS labelled
FROM evaluations
WHERE label IS NOT NULL AND confidence IS NOT NULL
GROUP BY schema_id, primary_question
ORDER BY labelled DESC;
```

Compare against `LAYA_STUDIO_CALIBRATION_MIN_SAMPLES` to see which surfaces can
be fitted.

### Retention

There is no automatic pruning. `evaluations` grows one row per decision, plus a
row per bulk row. For high-volume ingestion, prune by source and age:

```sql
DELETE FROM evaluations
WHERE source IN ('api', 'webhook')
  AND created_at < now() - interval '90 days';
```

Never delete rows that carry a `label`: they are the calibration history. Review
items and action logs cascade from their parents, but evaluations they point at are
`SET NULL` — the item survives with its copied `state`. See
[ROADMAP.md](ROADMAP.md#8-data-retention) for the planned retention tooling.
