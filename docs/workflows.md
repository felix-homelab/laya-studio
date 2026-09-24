# Workflows

The automation half of the panel: turn a decision into an action, and know when
*not* to.

## The ladder

Every workflow run evaluates a state, picks a decision question, and routes on its
confidence.

```
                          confidence
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
     >= 0.90              0.60 – 0.90             < 0.60
        │                     │                     │
      auto                 verify                 review
        │                     │                     │
  run steps with       ask a generative       run steps with
  on="auto"            model to adjudicate     on="review"
                             │
                   ┌─────────┴─────────┐
                   ▼                   ▼
              it agrees          it disagrees,
                   │             is unavailable,
             run auto steps      or is unparseable
                   │                   │
                   ▼                   ▼
              effective=auto      effective=review
```

Thresholds come from the workflow, defaulting to the globals
`LAYA_STUDIO_AUTO_THRESHOLD` (0.90) and `LAYA_STUDIO_VERIFY_THRESHOLD` (0.60).

### The rule that matters

**A failed verification is not a green light.** If no LLM is configured, the call
times out, the provider 500s, or the response is not parseable JSON, the item
escalates to human review. It never auto-executes on an unverified middle band.

Absence of evidence is not evidence of correctness. See
[ADR 0003](adr/0003-fallback-fails-closed.md).

The consequence for a fresh install with no provider configured: **the middle band
always escalates.** That is intentional and it is the safe default. Set
`LAYA_STUDIO_LLM_PROVIDER` to make the band do its job.

### `route` vs `effective_route`

Every run records both:

* `route` — what the ladder decided from the confidence alone;
* `effective_route` — what actually happened after verification.

They differ only in the middle band. Keeping both means the dashboard can show
you where the safety net caught something, rather than flattening it away.

### Which question decides

`decision_question` on the workflow. Left empty, `pick_primary_question` chooses:
first `choice` question, then first `noul`, then first `score`, then whatever
comes first. Explicit is better — the choice changes what your thresholds mean.

## Steps

A step is `{on, kind, config}`. The panel's `/workflows` page builds that object
for you: a **script** of colour-coded blocks, one stack per ladder rung, with a
dropdown inside each block for what it does and which route runs it. Blocks can
be dragged between stacks; the API contract below is what the builder emits.


| `on` | Runs when |
|---|---|
| `auto` | The resolved route is `auto` |
| `verify` | The resolved route is `verify` (before adjudication resolves it) |
| `review` | The resolved route is `review` |
| `always` | Every run, whatever the route |

| `kind` | Effect |
|---|---|
| `webhook` | POST/PUT JSON to a URL |
| `email` | Send via SMTP |
| `review` | Add an item to the human review queue |
| `noop` | Record that something happened, without side effects |

Steps run in declaration order. If **no** step matches the effective route, a
`noop` is logged saying so — a run is never a silent no-op.

A workflow with `steps: []` uses built-in defaults, so the ladder is visible from
the first run instead of appearing to do nothing.

### Templating

Every string in a step's `config` is formatted against the run context with
`str.format_map`. Unknown placeholders are **left intact** rather than blanked, so
a typo in a payload template shows up in the ActionLog instead of silently sending
an empty field.

| Placeholder | Value |
|---|---|
| `{workflow_key}`, `{workflow_name}`, `{workflow_id}` | |
| `{workflow_run_id}`, `{evaluation_id}` | |
| `{route}`, `{effective_route}` | |
| `{question}`, `{choice}`, `{confidence}` | |
| `{state_text}` | the flattened state |
| `{reason}` | why it was escalated |
| `{priority}` | 100 for `review`, else 0 |
| `{verification}` | the verdict dict, when one ran |

### Webhook steps

```json
{
  "on": "auto",
  "kind": "webhook",
  "config": {
    "url": "https://ops.example.com/hooks/laya",
    "method": "POST",
    "headers": {"X-Team": "support"},
    "payload": {
      "label": "{choice}",
      "confidence": "{confidence}",
      "workflow": "{workflow_key}",
      "state": "{state_text}"
    }
  }
}
```

Omit `payload` and a sensible default is sent (workflow, route, question, choice,
confidence, state, evaluation id).

#### SSRF protection

This is the one place the panel makes an outbound request to an operator-supplied
URL, so it is guarded. Before any request, `assert_public_target` refuses:

* any scheme other than `http`/`https`;
* any host resolving to a private, loopback, link-local, reserved, multicast or
  unspecified address — which covers `169.254.169.254` (the cloud metadata
  service), `127.0.0.1`, `10.0.0.0/8`, `192.168.0.0/16`, `::1` and friends.

Hosts you genuinely need can be opted in:

```bash
LAYA_STUDIO_WEBHOOK_ALLOWED_HOSTS=internal-api.corp.example,localhost
```

A blocked target is recorded as `status: "failed"` with an explanatory `error` on
the ActionLog — it never raises into the request that triggered it.

### Email steps

```json
{
  "on": "review",
  "kind": "email",
  "config": {
    "to": ["support-leads@example.com"],
    "subject": "Low-confidence decision: {choice}",
    "body": "Workflow {workflow_key} could not confidently classify:\n\n{state_text}"
  }
}
```

Requires `LAYA_STUDIO_EMAIL_ENABLED=true` plus SMTP settings. When email is
disabled the step logs `status: "skipped"` with the reason — visible in the UI, not
silently dropped.

### Review steps

```json
{ "on": "review", "kind": "review", "config": { "reason": "low_confidence", "priority": 50 } }
```

Higher `priority` sorts first in the queue. Reasons other than the built-in ones
are allowed and show up in the queue's `by_reason` breakdown.

## The verification prompt

When the route is `verify`, the engine asks a generative model to adjudicate the
*same* question over the *same* state:

```
Question: department
Instructions: Which department should handle this request?

Allowed options:
- billing
- technical
- sales
- other

State to classify:
---
subject: Duplicate charge on invoice #4411
body: We were billed twice...
---

A fast decision model answered 'billing' with 82.8% confidence. That is in the
uncertain band, so it needs adjudication.

Decide the single best option yourself. Reply with ONLY a JSON object:
{"choice": "<one of the allowed options>", "agrees": <true|false>, "rationale": "<one short sentence>"}
```

Only the label and a one-line rationale are requested — never free-form reasoning.
The point of the hybrid is to spend as few tokens as possible.

The response parser scans for the first **balanced** `{…}` object rather than
trusting the whole response to parse, because models wrap JSON in prose even when
told not to. A returned label that is not one of the offered options is recorded
with an explanatory `error` and treated as unverified.

`temperature=0` and `max_tokens` from `LAYA_STUDIO_LLM_MAX_TOKENS` (default 512).

## Triggers

### API / webhook

```bash
curl -X POST http://localhost:8000/api/ingest/email-triage-demo \
  -H 'Content-Type: application/json' \
  -H 'X-Ingest-Token: your-token' \
  -d '{"text": "We were billed twice and will cancel."}'
```

Or send a structured state:

```bash
curl -X POST http://localhost:8000/api/ingest/email-triage-demo \
  -H 'Content-Type: application/json' \
  -d '{"state": {"from": "a@b.c", "subject": "Refund", "body": "..."}}'
```

For senders that cannot emit JSON, a raw text body:

```bash
curl -X POST http://localhost:8000/api/ingest/email-triage-demo/text \
  --data-binary 'We were billed twice.'
```

### Dry run

```bash
-d '{"text": "...", "dry_run": true}'
```

Evaluates and returns the route the ladder **would** pick, executing no actions and
creating no run. Use it to wire a caller up before going live.

### Email

```bash
curl -X POST http://localhost:8000/api/ingest/email/email-triage-demo \
  -H 'Content-Type: application/json' \
  -d '{"from": "dana@acme.example", "subject": "Refund please", "body": "We were double charged."}'
```

The payload becomes a structured state (`from`, `to`, `subject`, `body`), so the
model sees the same fields a real triage worker would give it.

### Authentication

If `LAYA_STUDIO_INGEST_TOKEN` is set, every `/api/ingest/*` route requires a
matching `X-Ingest-Token` header, compared in constant time. When it is unset,
ingestion is open — fine for a laptop, called out in the UI and in
[deployment.md](deployment.md#hardening).

Ingestion is the only authenticated surface. The rest of the panel is an operator
tool and assumes it sits behind your own access control.

## Response shape

```json
{
  "dry_run": false,
  "run": { "id": 12, "status": "succeeded", "route": "verify", "confidence": 0.74, ... },
  "route": "verify",
  "effective_route": "auto",
  "verification": {
    "status": "ok",
    "choice": "billing",
    "agrees": true,
    "rationale": "The sender describes a duplicate charge and asks for a refund.",
    "provider": "openai",
    "model": "gpt-4o-mini",
    "latency_ms": 812.4
  },
  "actions": [{ "kind": "noop", "status": "ok", "detail": "Filed to billing automatically (0.74)." }],
  "evaluation": { "id": 88, "top_choice": "billing", "confidence": 0.74, ... },
  "error": null
}
```

`verification.status` is `ok`, `skipped` (no provider / no key) or `failed`
(network, HTTP error, unparseable output).

## Worked example: email triage with a human safety net

```bash
curl -X POST localhost:8000/api/workflows -H 'Content-Type: application/json' -d '{
  "name": "Inbound email triage",
  "schema_id": 1,
  "trigger_type": "webhook",
  "decision_question": "department",
  "auto_threshold": 0.92,
  "verify_threshold": 0.70,
  "steps": [
    {
      "on": "auto",
      "kind": "webhook",
      "config": {
        "url": "https://ops.example.com/hooks/route",
        "payload": {"queue": "{choice}", "confidence": "{confidence}", "state": "{state_text}"}
      }
    },
    { "on": "auto", "kind": "noop", "config": { "detail": "Routed to {choice} at {confidence}." } },
    { "on": "review", "kind": "review", "config": { "reason": "low_confidence", "priority": 60 } },
    {
      "on": "review",
      "kind": "email",
      "config": {
        "to": ["triage-leads@example.com"],
        "subject": "Needs a human: {choice} at {confidence}",
        "body": "{state_text}"
      }
    }
  ]
}'
```

Then feed it:

```bash
for text in \
  "We were billed twice, please refund the duplicate charge today." \
  "Uh, not sure what this is about" \
  "PRODUCTION IS DOWN, our integration returns a 500 since this morning"
do
  curl -s -X POST localhost:8000/api/ingest/inbound-email-triage \
    -H 'Content-Type: application/json' \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"text": sys.argv[1]}))' "$text")" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["route"], "->", d["effective_route"], [a["kind"] for a in d["actions"]])'
done
```

## Watching a run

Every action is persisted, so the UI can show exactly what fired without
re-deriving it:

```bash
curl -s localhost:8000/api/workflows/runs/12 | python3 -m json.tool
```

The response includes the full `action_logs` array and the `evaluation`.

`GET /api/workflows/runs` lists across all workflows, filterable by `route` — the
fastest way to answer "what did the safety net catch today?"

## Operational notes

**Idempotency.** Runs are not idempotent. Re-posting the same state creates a new
evaluation and a new run. If your caller retries, de-duplicate on your side.

**Latency.** A run is synchronous: one Laya forward pass (~33 ms) plus any
verification (hundreds of ms) plus actions. Set your caller's timeout
accordingly — a `verify` route with a slow provider is the worst case.

**Failure isolation.** A failing action is logged and the run continues. The run
still reports `succeeded`; inspect `actions[].status` for what actually worked.

**A run that cannot start.** No schema, a disabled workflow, or an engine error
produces `status: "failed"` with `route: "error"` and an explanatory `error`
field — never a 500.

**Disabled workflows refuse to run** rather than silently doing nothing. That is
deliberate: a disabled workflow that still fired would be a much worse bug.
