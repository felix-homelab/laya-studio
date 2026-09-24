# API reference

Complete, endpoint by endpoint. For the generated schema, use
<http://localhost:8000/api/docs> (Swagger), `/api/redoc` or `/api/openapi.json`.

61 endpoints across ten routers, plus the MCP endpoint at `/mcp` (a different
protocol, so it is not in the OpenAPI schema). Everything under `/api` returns
JSON; everything else is HTML.

## Conventions

**Errors.** FastAPI's standard shape:

```json
{"detail": "Which department should handle this request?"}
```

| Status | Means |
|---|---|
| 400 | The request was understood and refused (`ValueError` from a service) |
| 401 | Missing or invalid `X-Ingest-Token`, or `X-Observability-Token` on `/api/observability/*` |
| 404 | No such resource |
| 409 | The resource exists but the operation is not allowed (deleting a template, rerunning a running job, retrying an item with no workflow) |
| 413 | Upload exceeded `LAYA_STUDIO_MAX_UPLOAD_MB` |
| 422 | Validation failed — malformed body, a question bundle that does not lint, or a schema Laya cannot render (see `remedy`) |
| 503 | Laya is not usable. `state: "loading"` is temporary (respect `Retry-After`); `state: "failed"` is permanent for this process |

**Correlation.** Every response carries `X-Request-ID`. Quote it from a bug report;
`GET /api/observability/logs?request_id=...` returns every log line that request
produced.

**Timestamps** are ISO-8601 UTC.

**Confidence fields** are floats in `[0, 1]`, not percentages. The UI multiplies.

**`confidence` vs `raw_confidence`.** `confidence` is what the model reported
*after* the calibration map was applied. `raw_confidence` is the unmodified value.
`calibrated` says which path was taken; `calibration_slope` and
`calibration_intercept` describe the map (`p' = sigmoid(slope·logit(p) + intercept)`).
See [analytics-and-calibration.md](analytics-and-calibration.md#the-calibration-map).

**`band`** is `act` \| `verify` \| `escalate` \| `unknown`, derived from
`confidence` against the live thresholds. `act_probability` is Laya's separate
opinion and may disagree.

**Engine provenance.** Every evaluation carries `engine` (always `laya`) and
`engine_model` — the checkpoint that answered: `english` | `multilingual` |
`typed-decisions`. The checkpoint is read from Laya's `routing["model"]`, not from
the top-level `model` key, which is Laya's internal agent class name.

**`confidence` is an entropy measure.** Laya reports `1 - H(p)/log(k)`, not the
probability of the winning label. For a 4-way `choice` the two differ by ~15 points.
The ladder gates on `confidence`; the `breakdown` carries the actual probabilities.

---

## Health — `/api`

### `GET /api/health`

**Liveness.** Always 200 while the process is serving — *including while Laya is
still loading*, because the panel really is up. Read `status` to tell the three
cases apart:

| `status` | Meaning |
|---|---|
| `starting` | The engine is loading. Decisions return 503. The panel is fully browsable. |
| `ok` | The engine is ready. |
| `degraded` | The runtime could not be built at all. Decisions return 503 permanently. |

```json
{
  "status": "starting",
  "app": "Laya Studio",
  "version": "0.1.0",
  "environment": "production",
  "engine": {
    "state": "loading",
    "ready": false,
    "error": null,
    "name": "laya",
    "device": null,
    "loaded_checkpoints": [],
    "duration_seconds": null,
    "detail": {}
  },
  "llm": {"provider": "none", "enabled": false, "model": null},
  "thresholds": {"auto": 0.9, "verify": 0.6},
  "database": {
    "dialect": "postgresql",
    "reachable": true,
    "tables": {"schemas": 4, "evaluations": 0, "bulk_jobs": 0, "ab_tests": 0,
               "workflows": 0, "workflow_runs": 0, "review_items": 0,
               "calibration_snapshots": 0}
  }
}
```

Once ready, `engine` is a pass-through of the adapter's own description:

```json
{
  "state": "ready", "ready": true, "error": null, "name": "laya",
  "device": "mps", "loaded_checkpoints": ["english", "multilingual"],
  "duration_seconds": 42.1,
  "detail": {"preload": true, "max_loaded": 2, "head_max_len": null, "startup_seconds": 42.1}
}
```

### `GET /api/ready`

**Readiness.** 503 with `Retry-After: 10` until the engine can serve a decision,
then 200. Route traffic on this, not on `/health`, or a replica that answers every
evaluation with a 503 will be marked healthy.

```json
{"ready": false, "engine": {"state": "loading", "ready": false, "error": null, "...": "..."}}
```

---

## Playground — `/api/playground`

### `POST /api/playground/evaluate`

Score one state. The endpoint behind the playground form.

| Field | Type | Notes |
|---|---|---|
| `state` | object \| string | **required**. A string, or a dict (email, ticket, arbitrary JSON). |
| `schema_id` | int | Provide this or `schema_key` or `questions`. |
| `schema_key` | string | Slug lookup. |
| `questions` | object | Inline bundle. **Overrides the schema's questions** — this is how the editor previews unsaved edits. |
| `model` | string | `english` \| `multilingual` \| `typed-decisions`. Omit to let the router decide. |
| `primary_question` | string | Which question drives `top_choice`/`confidence`. Defaults to the first `choice`, else first `noul`, else first `score`. |
| `apply_calibration` | bool | Default `true`. |
| `persist` | bool | Default `true`. `false` gives a stateless preview and returns `id: null`. |

```bash
curl -s localhost:8000/api/playground/evaluate -H 'Content-Type: application/json' -d '{
  "state": {"subject": "Duplicate charge", "body": "We were billed twice. Refund or we cancel."},
  "schema_id": 1,
  "primary_question": "department"
}' | python3 -m json.tool
```

```json
{
  "id": 1,
  "schema_id": 1,
  "state": {"subject": "Duplicate charge", "body": "We were billed twice. Refund or we cancel."},
  "state_text": "subject: Duplicate charge\nbody: We were billed twice. Refund or we cancel.",
  "question_results": [
    {
      "question": "department",
      "type": "choice",
      "instructions": "Which department should handle this request?",
      "choice": "billing",
      "level": null,
      "confidence": 0.7989,
      "raw_confidence": 0.7989,
      "calibrated": false,
      "temperature": 1.0,
      "noul": null,
      "score": null,
      "max_score": null,
      "act_probability": 1.0,
      "rubric": ["billing", "technical", "sales", "other"],
      "breakdown": [
        {"label": "billing", "raw_label": "billing", "probability": 0.9432, "rank": 1, "is_top": true},
        {"label": "sales", "raw_label": "sales", "probability": 0.0259, "rank": 2, "is_top": false}
      ],
      "alternatives": [{"label": "sales", "probability": 0.0259, "rank": 2, "is_top": false}],
      "band": "verify"
    }
  ],
  "routing": {
    "model": "english",
    "repo": "convaiinnovations/laya",
    "reason": "latin script (100% of letters)",
    "script": "latin",
    "script_fraction": 1.0,
    "forced": false
  },
  "engine": "laya",
  "engine_model": "english",
  "primary_question": "department",
  "top_choice": "billing",
  "confidence": 0.7989,
  "raw_confidence": 0.7989,
  "calibrated": false,
  "latency_ms": 34.1,
  "source": "playground",
  "label": null,
  "created_at": "2026-01-01T12:00:00Z",
  "warnings": [],
  "calibration": {}
}
```

Per-question `type` determines what to read:

| `type` | Read | Null by design |
|---|---|---|
| `choice` | `choice`, `breakdown`, `alternatives` | `noul`, `score`, `level`, `max_score` |
| `score` | `score` (expected level), `level` (most likely level), `max_score`, `rubric`, `breakdown` | `choice`, `noul` |
| `noul` | `noul` (calibrated `P(true)`) | `choice`, `score`, `breakdown` (empty — a binary head has no distribution) |

Fields are gated on the primitive rather than populated opportunistically, so a
`score` question never reports a `choice` invented from the argmax of its
distribution.

For `score`, `breakdown[].label` is the rubric text and `raw_label` is Laya's index
key (`"0"`, `"1"`, …) — Laya returns them in a separate `legend`, which Laya Studio
maps back so a breakdown shows the levels you wrote.

`act_probability` is Laya's own trained act/escalate head, when the checkpoint emits
one. It is displayed alongside the band but does **not** drive routing.

`band` is `act` \| `verify` \| `escalate` \| `unknown`, derived from `confidence`
against the live thresholds.

**422** when the bundle does not lint, with the reason in `detail`:

```json
{"detail": "q: choice requires a criteria object with at least two labels."}
```

Or when Laya cannot physically render the options, in which case `remedy` carries
the fix:

```json
{
  "detail": "question 'q' options exceed head_max_len=192. Reduce the number of options, shorten their descriptions, or shortlist them. Raising LAYA_STUDIO_LAYA_HEAD_MAX_LEN only helps if LAYA_STUDIO_LAYA_MAX_LEN is raised with it — the budget must fit inside the sequence limit.",
  "remedy": "Reduce the number of options, shorten their descriptions, or shortlist them. ..."
}
```

### `POST /api/playground/route`

Which checkpoint *would* serve a state, without scoring it.

```bash
curl -s localhost:8000/api/playground/route -H 'Content-Type: application/json' \
  -d '{"state": {"text": "同じ月に二回請求されています。"}}'
```

```json
{"routing": {
  "model": "multilingual",
  "repo": "convaiinnovations/laya/multilingual",
  "reason": "non-Latin script (han, 100% of letters); the English checkpoint cannot read it",
  "script": "cjk",
  "script_fraction": 1.0,
  "forced": false
}}
```

Cheap enough to call on every keystroke; the playground debounces it.

### `POST /api/playground/lint`

Lint a question bundle. Deliberately **permissive**: it accepts malformed input so
it can tell you *what* is malformed.

```bash
curl -s localhost:8000/api/playground/lint -H 'Content-Type: application/json' \
  -d '{"questions": {"q": {"type": "choice", "criteria": {"only": "one"}}}}'
```

```json
{
  "ok": false,
  "findings": [
    {"severity": "error", "question": "q",
     "message": "choice requires a criteria object with at least two labels."}
  ]
}
```

Findings: `severity` is `error` (blocks saving) or `warning`. There are 11 rules:

| Rule | Severity |
|---|---|
| At least one question | error |
| Definition is an object | error |
| `type` ∈ `choice`\|`score`\|`noul` | error |
| `choice.criteria` is an object with ≥ 2 non-empty labels | error |
| `score.criteria` is a list with ≥ 2 levels | error |
| Question name > 120 characters | warning |
| Missing `instructions` | warning |
| `choice` with >= 11 options — Laya's `choice:11+` temperature bucket ships out of range and is clamped, so confidence there is uncalibrated | warning |
| `score` with >= 11 levels — levels are truncated to a fixed token budget, so adjacent levels stop being distinguishable | warning |
| `noul` yes/no criteria sharing distinctive vocabulary | warning |

The last rule catches the most common binary-guardrail authoring mistake: "threatens
to cancel" / "does not threaten to cancel" gives a one-pass decision head almost
nothing to separate on.

### `GET /api/playground/history?limit=25&schema_id=`

Recent playground evaluations, newest first. Only `source == "playground"`.

### `GET /api/playground/evaluations/{id}`

Any evaluation by id, whatever door it came through.

### `POST /api/playground/evaluations/{id}/label`

Attach ground truth.

```bash
curl -X POST localhost:8000/api/playground/evaluations/1/label \
  -H 'Content-Type: application/json' -d '{"label": "billing", "label_source": "human"}'
```

Returns the updated evaluation. Labelled rows are what the calibration monitor
fits on.

---

## Schemas — `/api/schemas`

### `GET /api/schemas?include_templates=true&search=&tag=`

Summaries (no `definition`), templates first, then alphabetical.

### `POST /api/schemas`

```bash
curl -X POST localhost:8000/api/schemas -H 'Content-Type: application/json' -d '{
  "name": "Support triage",
  "description": "Route by team and flag risk",
  "tags": ["support"],
  "questions": {
    "team": {
      "type": "choice",
      "instructions": "Which team should own this?",
      "criteria": {"billing": "invoices and refunds", "technical": "bugs and outages"}
    },
    "risk": {
      "type": "noul",
      "instructions": "Is the customer threatening to leave?",
      "criteria": {"yes": "warns they will take their business elsewhere",
                   "no": "describes a problem only"}
    }
  }
}'
```

`201` with the created schema. `key` is derived from `name`, slugified and
de-duplicated (`support-triage`, `support-triage-2`). `primitive` is derived:
`choice`/`score`/`noul` for a single-primitive schema, `mixed` otherwise.
`current_revision` starts at 1.

**422** if any question fails lint, with all errors joined in `detail`.

### `GET /api/schemas/{id}`

Includes `definition`. This is what the editors load.

### `PUT /api/schemas/{id}`

Partial update. **Metadata-only edits do not bump the revision; changing
`questions` does.**

```bash
# Rename: revision stays at 1
curl -X PUT localhost:8000/api/schemas/5 -H 'Content-Type: application/json' \
  -d '{"name": "Support triage v2"}'

# Reword a question: revision becomes 2
curl -X PUT localhost:8000/api/schemas/5 -H 'Content-Type: application/json' \
  -d '{"questions": {"team": {"type": "choice", "instructions": "Which queue?", "criteria": {"billing": "invoices", "technical": "bugs"}}}, "note": "clarified wording"}'
```

The `note` is stored on the new revision — use it to record *why*.

### `GET /api/schemas/{id}/revisions`

Newest first, each with its frozen `definition`. This is the input to A/B testing.

### `POST /api/schemas/{id}/fork?name=`

Copy a schema — typically a template — into an editable, non-template copy.

```bash
curl -X POST "localhost:8000/api/schemas/1/fork?name=My%20triage"
```

`201` with the new schema. This is the only way to edit a built-in template.

### `DELETE /api/schemas/{id}`

`204`. **409 for templates** — fork instead. Evaluations referencing the schema get
`schema_id = NULL` but keep their own copy of the questions, so history survives.

### `GET /api/schemas/{id}/lint`

Same rules as `/api/playground/lint`, applied to a stored schema.

---

## Bulk testing — `/api/bulk`

### `POST /api/bulk/upload`

`multipart/form-data`:

| Field | Notes |
|---|---|
| `file` | **required**. `.csv` or `.json`. |
| `schema_id` | **required**. |
| `name` | Defaults to the filename. |
| `state_column` | Send one column as free text (`{"text": ...}`). Omit and every column becomes the structured state. |
| `label_column` | Enables accuracy, ECE and band counts. |
| `model` | Checkpoint override. |

```bash
curl -s -X POST localhost:8000/api/bulk/upload \
  -F file=@testset.csv -F schema_id=1 -F label_column=truth | python3 -m json.tool
```

```json
{
  "id": 1, "name": "testset.csv", "status": "succeeded",
  "total_rows": 4, "processed_rows": 4, "failed_rows": 0,
  "summary": {
    "total": 4, "processed": 4, "failed": 0, "labelled": 4,
    "mean_confidence": 0.512, "accuracy": 0.75, "ece": 0.443, "mce": 0.5,
    "brier": 0.398, "band_counts": {"auto": 0, "verify": 1, "review": 3},
    "prediction_distribution": {"billing": 1, "technical": 1, "sales": 1, "other": 1},
    "calibration_note": "", "errors": []
  },
  "items": [{"row_index": 0, "state": {...}, "label": "billing",
             "predicted": "billing", "confidence": 0.71, "ok": true, "error": null}]
}
```

Metric keys are **present-but-null** when they cannot be computed, so clients never
have to guess whether a key exists.

Formats: CSV with a header row; JSON as a list of objects, a list of strings, or an
object with a `rows`/`items`/`data`/`records`/`states` key. Rows are decoded as
UTF-8 (with or without BOM) or Latin-1.

Limits: 5,000 rows (`MAX_BULK_ROWS`) and `LAYA_STUDIO_MAX_UPLOAD_MB` (16).

**413** past the size cap. **422** for an empty file, invalid JSON (with line
number), or too many rows.

> Runs **synchronously** — the request stays open while every row is scored. A
> 5,000-row job against real Laya is minutes. See
> [ROADMAP.md](ROADMAP.md#the-worker-path).

### `GET /api/bulk/jobs?limit=50`

Summaries, newest first.

### `GET /api/bulk/jobs/{id}?include_items=true&item_limit=200`

Full job including the item preview.

### `GET /api/bulk/jobs/{id}/items?offset=&limit=`

Paginated rows.

### `POST /api/bulk/jobs/{id}/rerun`

Scores every row again — useful after a calibration refit, since each row gets a
fresh evaluation. **Not idempotent:** it creates new `evaluations` rows.

### `DELETE /api/bulk/jobs/{id}`

`204`. Cascades to items; evaluations survive.

---

## A/B testing — `/api/ab`

### `POST /api/ab/tests`

Registers and **runs** a comparison.

| Field | Notes |
|---|---|
| `name` | required |
| `schema_id` | required — both revisions must belong to it |
| `revision_a_id`, `revision_b_id` | required, and must differ |
| `dataset` | List of `{"state": {...}, "label": "..."}` |
| `source_job_id` | Alternative to `dataset`: reuse a bulk job's rows |
| `model` | Checkpoint override |

```bash
curl -s -X POST localhost:8000/api/ab/tests -H 'Content-Type: application/json' -d '{
  "name": "Reworded routing question",
  "schema_id": 5, "revision_a_id": 9, "revision_b_id": 10,
  "dataset": [{"state": {"body": "we were billed twice"}, "label": "billing"}]
}' | python3 -m json.tool
```

```json
{
  "id": 1, "status": "succeeded",
  "summary": {
    "total": 4, "flip_count": 0, "flip_rate": 0.0, "agreement": 1.0,
    "mean_confidence_a": 0.51, "mean_confidence_b": 0.51, "mean_confidence_delta": 0.0,
    "accuracy_a": 0.75, "accuracy_b": 0.75, "accuracy_delta": 0.0,
    "fixed_by_b": 0, "broken_by_b": 0, "net_gain": 0,
    "labelled": 4,
    "transitions": [],
    "interpretation": "No answers changed. The two revisions are behaviourally identical on this dataset, so any accuracy difference you see elsewhere is sampling noise."
  },
  "items": [{"row_index": 0, "choice_a": "billing", "choice_b": "billing",
             "confidence_a": 0.71, "confidence_b": 0.71, "flipped": false,
             "result_a": {...}, "result_b": {...}}]
}
```

How to read `flip_rate`, `net_gain` and `transitions`:
[analytics-and-calibration.md](analytics-and-calibration.md#reading-an-ab-test).

**422** when both revisions are the same, when they belong to different schemas,
when no dataset is supplied, or when the dataset exceeds 2,000 rows.

### `GET /api/ab/tests?schema_id=&limit=50`

Summaries, newest first.

### `GET /api/ab/tests/{id}`

Full test including every item.

### `POST /api/ab/tests/{id}/run`

Re-runs against the stored dataset.

### `DELETE /api/ab/tests/{id}`

`204`.

---

## Workflows — `/api/workflows`

### `GET /api/workflows?enabled_only=false`

All workflows. `steps`, thresholds, and `trigger_type` included.

### `POST /api/workflows`

```bash
curl -s -X POST localhost:8000/api/workflows -H 'Content-Type: application/json' -d '{
  "name": "Inbound triage",
  "schema_id": 1,
  "trigger_type": "webhook",
  "decision_question": "department",
  "auto_threshold": 0.92,
  "verify_threshold": 0.70,
  "steps": [
    {"on": "auto", "kind": "webhook",
     "config": {"url": "https://ops.example.com/hook",
                "payload": {"queue": "{choice}", "confidence": "{confidence}"}}},
    {"on": "review", "kind": "review", "config": {"reason": "low_confidence", "priority": 60}}
  ]
}' | python3 -m json.tool
```

`steps[].on` ∈ `auto` \| `verify` \| `review` \| `always`.
`steps[].kind` ∈ `webhook` \| `email` \| `review` \| `noop`.
Unknown values are **422**, not silently ignored.

`verify_threshold` is clamped to `auto_threshold`. Thresholds default to the
global settings.

### `GET /api/workflows/stats`

```json
{"total": 2, "enabled": 2, "runs_by_route": {"auto": 6, "verify": 28, "review": 55}}
```

> Declared before `/{workflow_id}` so path matching never has to guess whether
> `stats` is an id.

### `GET /api/workflows/{id}`

### `PUT /api/workflows/{id}`

Partial update. Sending `steps` replaces the list wholesale.

### `DELETE /api/workflows/{id}`

`204`. Cascades to runs and action logs. Review items are `SET NULL` and survive.

### `POST /api/workflows/{id}/run`

Manually fire a workflow — the `manual` trigger.

```bash
curl -s -X POST localhost:8000/api/workflows/1/run -H 'Content-Type: application/json' \
  -d '{"text": "We were billed twice and will cancel."}' | python3 -m json.tool
```

```json
{
  "run": {"id": 12, "status": "succeeded", "route": "verify", "confidence": 0.74, ...},
  "route": "verify",
  "effective_route": "auto",
  "verification": {"status": "ok", "choice": "billing", "agrees": true, ...},
  "actions": [{"kind": "noop", "status": "ok", "detail": "..."}],
  "evaluation": {"id": 88, "top_choice": "billing", ...},
  "error": null
}
```

### `GET /api/workflows/{id}/runs?route=&limit=100`

### `GET /api/workflows/runs?route=&limit=100`

Across all workflows. The fastest way to see what the safety net caught.

### `GET /api/workflows/runs/{id}`

Includes `action_logs` (every action, with request and response) and the
`evaluation`.

### `GET /api/workflows/runs/{id}/evaluation`

Just the evaluation.

---

## Ingestion — `/api/ingest`

Every route here is guarded by `X-Ingest-Token` **when `LAYA_STUDIO_INGEST_TOKEN`
is set**. When it is unset, they are open.

### `POST /api/ingest/{workflow_key}`

| Field | Notes |
|---|---|
| `state` | Object or string. |
| `text` | Convenience for plain text. One of `state`/`text` is required. |
| `metadata` | Free-form; persisted on the run for auditing. |
| `dry_run` | `false` by default. `true` evaluates and returns the route **without** executing actions or creating a run. |

```bash
# Live
curl -s -X POST localhost:8000/api/ingest/inbound-triage \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Token: your-token' \
  -d '{"text": "We were billed twice and will cancel."}'

# Dry run, to validate a caller before going live
curl -s -X POST localhost:8000/api/ingest/inbound-triage \
  -H 'Content-Type: application/json' \
  -d '{"text": "...", "dry_run": true}'
```

```json
{"dry_run": true, "route": "verify", "effective_route": "verify",
 "verification": null, "actions": [], "run": null,
 "evaluation": {...}, "error": null}
```

**401** with a wrong or missing token. **404** for an unknown key. **422** with
neither `state` nor `text`.

### `POST /api/ingest/{workflow_key}/text`

Raw `text/plain` body, for senders that cannot emit JSON.

```bash
curl -X POST localhost:8000/api/ingest/inbound-triage/text \
  --data-binary 'We were billed twice.'
```

Sent as `{"text": "<body>"}`. **422** for an empty body.

### `POST /api/ingest/email/{workflow_key}`

A parsed inbound email.

```bash
curl -s -X POST localhost:8000/api/ingest/email/inbound-triage \
  -H 'Content-Type: application/json' \
  -d '{"from": "dana@acme.example", "to": "support@example.com",
       "subject": "Duplicate charge", "body": "We were billed twice."}'
```

Becomes `{"from": ..., "to": ..., "subject": ..., "body": ...}` with empty fields
dropped, so the model sees the same structured state a triage worker would build.
`source` on the resulting evaluation is `email`.

---

## Analytics — `/api/analytics`

`hours` is 1–8760, default 168 (7 days). `bucket` is `hour` or `day`.

### `GET /api/analytics/dashboard?hours=168&bucket=hour`

Everything below in one call — what the analytics page renders.

```json
{
  "overview": {...}, "histogram": [...], "timeseries": [...],
  "routes": {...}, "calibration": {...},
  "thresholds": {"auto": 0.9, "verify": 0.6}
}
```

### `GET /api/analytics/overview`

```json
{
  "window": {"hours": 168, "start": "...", "end": "..."},
  "total": 320, "calibrated_share": 0.06,
  "by_source": {"api": 260, "playground": 60},
  "by_model": {"english": 300, "multilingual": 20},
  "confidence": {"mean": 0.49, "median": 0.44, "p95": 0.88},
  "latency_ms": {"mean": 0.41, "median": 0.33, "p95": 1.1},
  "bands": {"auto": 6, "verify": 119, "review": 264},
  "escalation_rate": 0.68, "verify_rate": 0.31, "auto_rate": 0.015,
  "labelled": 300, "accuracy": 0.83, "ece": 0.383, "brier": 0.29,
  "calibration_note": "", "top_choices": [{"label": "billing", "count": 80}]
}
```

Returns `{"total": 0, "note": "..."}` when the window is empty rather than a
structure full of nulls.

### `GET /api/analytics/timeseries?hours=168&bucket=day`

```json
[{"bucket": "2026-01-01T00:00:00Z", "count": 48, "mean_confidence": 0.51,
  "auto": 2, "verify": 18, "review": 28, "escalation_rate": 0.58}]
```

### `GET /api/analytics/routes`

```json
{
  "window": {...}, "runs": 89,
  "by_route": {"auto": 6, "verify": 28, "review": 55},
  "by_status": {"succeeded": 89},
  "action_outcomes": {"noop:ok": 40, "review:ok": 55},
  "review_items": 60, "review_open": 55, "correction_rate": 0.0
}
```

### `GET /api/analytics/calibration?hours=`

ECE, MCE, Brier, the reliability `bins`, the daily `trend`, the latest
`snapshots`, and a plain-English `interpretation`. See
[analytics-and-calibration.md](analytics-and-calibration.md).

When there are no labelled rows:

```json
{"n_samples": 0, "note": "No labelled rows in this window. Label rows via an upload with a label column, the review queue, or the API."}
```

### `GET /api/analytics/calibration/report?schema_id=&question=`

On-demand report for one slice, without touching the dashboard.

### `POST /api/analytics/calibration/refit`

Refit every `(schema, question)` slice that has at least
`LAYA_STUDIO_CALIBRATION_MIN_SAMPLES` labelled rows. Clears the temperature cache.

```json
{"snapshots": [{"id": 1, "question": "department", "schema_id": 1,
                "slope": 0.4277, "intercept": 2.1622, "temperature": 2.338,
                "n_samples": 40, "ece": 0.4462,
                "accuracy": 0.85, "mean_confidence": 0.404}],
 "message": "Refit 1 slice(s)."}
```

`{"snapshots": [], "message": "Nothing to refit: ..."}` when nothing qualifies —
a 200, not an error.

### `GET /api/analytics/thresholds`

The live ladder, plus calibration settings.

---

## Review queue — `/api/review`

### `GET /api/review/stats`

```json
{
  "total": 60, "open": 55,
  "by_status": {"pending": 55, "corrected": 5},
  "by_reason": {"low_confidence": 55, "moderation_uncertain": 5},
  "resolved": 5, "reviewed": 5, "correction_rate": 1.0,
  "mean_open_confidence": 0.44, "oldest_open": "2026-01-01T09:00:00Z"
}
```

`correction_rate` is `corrected / (approved + corrected)` — how often the model was
wrong when a human looked. `null` until something is reviewed.

### `GET /api/review/items?status=&workflow_id=&reason=&search=&limit=&offset=`

`status=open` is a convenience alias for `pending` + `in_review`. `search`
substring-matches `state_text`. Ordered by `priority` desc, then oldest first.

### `GET /api/review/items/{id}`

Full item: state, model answer, confidence, and the ranked `alternatives`.

### `POST /api/review/items/{id}/claim`

```json
{"assignee": "dana"}
```

Sets `status: "in_review"`.

### `POST /api/review/items/{id}/resolve`

```json
{"status": "corrected", "corrected_label": "sales", "notes": "not billing at all"}
```

| `status` | Label written to the evaluation |
|---|---|
| `approved` | the model's own `model_choice` |
| `corrected` | `corrected_label` (**required**) |
| `rejected` | none — excluded from calibration |
| `in_review` | none |

**This is the feedback loop.** The label written here is what the next
`POST /api/analytics/calibration/refit` fits on. See
[data-model.md](data-model.md#review_items).

**422** for an unknown status or a `corrected` without a label.

### `POST /api/review/items/{id}/retry`

Re-runs the owning workflow against the same state — after a taxonomy change, say.

```json
{"run_id": 41, "route": "auto", "effective_route": "auto", "actions": [...], "error": null}
```

**409** when the item has no workflow, or the workflow no longer exists.

---

## Observability — `/api/observability`

Read-only over the JSONL log store. `401` on all of them when
`LAYA_STUDIO_OBSERVABILITY_TOKEN` is set and the `X-Observability-Token` header is
missing or wrong. See [observability-and-mcp.md](observability-and-mcp.md).

`level` is a **floor**, not an equality: `level=WARNING` returns WARNING, ERROR and
CRITICAL. `q` is a case-insensitive substring over the message, logger, path and
`mcp_tool`. Times are ISO-8601. Tracebacks are opt-in
(`include_traceback=false` by default) and capped at 12 000 characters.

### `GET /api/observability/logs`

```bash
curl -s 'localhost:8000/api/observability/logs?kind=exception&since=2026-09-22T09:00:00Z&limit=20' | jq
```

```json
{
  "records": [
    {
      "ts": "2026-09-22T11:24:58.101+00:00",
      "kind": "exception",
      "level": "ERROR",
      "logger": "laya_studio.request",
      "message": "POST /api/workflows/3/run raised ConnectionRefusedError: [Errno 111]",
      "request_id": "7c1f5b0a9d2e4411",
      "method": "POST",
      "path": "/api/workflows/3/run",
      "status": null,
      "duration_ms": 812.4,
      "mcp_tool": "workflow_run",
      "exc": {
        "type": "ConnectionRefusedError",
        "message": "[Errno 111] Connection refused",
        "location": "/app/app/services/actions.py:118",
        "fingerprint": "9f2c1ab0d4e77c31",
        "traceback_chars": 1842
      }
    }
  ],
  "count": 1,
  "scanned": 412,
  "truncated": false
}
```

`scanned` is how many records the query looked at and `truncated` says whether the
window or `limit` cut the answer short — so an empty `records` list is never
ambiguous.

**Filters:** `level`, `kind` (`log` \| `request` \| `exception`), `logger`, `q`,
`request_id`, `path`, `status_min`, `status_max`, `has_exception`, `fingerprint`,
`since`, `until`, `limit` (1–1000), `order` (`newest` \| `oldest`),
`include_traceback`.

### `GET /api/observability/logs/tail?lines=50&include_traceback=false`

The most recent records of any kind. The first thing to read.

### `GET /api/observability/errors?since=&limit=50&include_traceback=false`

Distinct exceptions, grouped by `fingerprint` (exception type + raise site, *not*
the message), newest first:

```json
{
  "groups": [
    {
      "fingerprint": "9f2c1ab0d4e77c31",
      "type": "ConnectionRefusedError",
      "location": "/app/app/services/actions.py:118",
      "message": "[Errno 111] Connection refused",
      "count": 37,
      "first_seen": "2026-09-22T10:02:11.004+00:00",
      "last_seen": "2026-09-22T11:24:58.101+00:00",
      "paths": ["/api/workflows/3/run"],
      "last_request_id": "7c1f5b0a9d2e4411"
    }
  ],
  "count": 1,
  "scanned": 412
}
```

### `GET /api/observability/errors/{fingerprint}?limit=20`

Every recorded occurrence of that group, tracebacks included. **404** with a hint
when the fingerprint is not in the retained window.

### `GET /api/observability/requests?status_min=&path=&limit=100`

The request timeline: `method`, `path`, `status`, `duration_ms`, `request_id`,
`mcp_tool` when an agent made the call.

### `GET /api/observability/stats`

Files and sizes on disk, counts by level and kind, oldest and newest timestamp, and
the rotation/retention settings — enough to know whether the log is worth reading.

---

## MCP — `/mcp`

Not REST: the Model Context Protocol's Streamable HTTP transport, serving 60 tools
that mirror this API plus the log store and read-only SQL. `POST` JSON-RPC to
`/mcp`; stateless, so no session header is required. **401** without the bearer token
when `LAYA_STUDIO_MCP_TOKEN` is set, **421** from a `Host` header that is not
declared, **503** before startup. Full tool list:
[observability-and-mcp.md](observability-and-mcp.md#the-tools).

---

## HTML routes

| Path | Page |
|---|---|
| `/` | Playground |
| `/schemas` | Schema library |
| `/bulk` | Bulk testing |
| `/ab` | A/B testing |
| `/workflows` | Workflows |
| `/analytics?hours=&bucket=` | Analytics dashboard |
| `/review?status=open` | Review queue |
| `/static/*` | Assets |

Page routes are excluded from the OpenAPI schema.
