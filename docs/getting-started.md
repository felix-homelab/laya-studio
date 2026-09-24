# Getting started

From zero to a running panel that is making real decisions.

## 1. Install

Two hard dependencies, neither optional: **Laya** (there is no fallback engine) and
**PostgreSQL** (there is no SQLite path).

### Docker (recommended)

```bash
HF_CACHE=~/.cache/huggingface docker compose up --build
```

Three services come up in order: `db`, then `migrate` (which applies Prisma's
migrations and exits), then `app`. Mounting the Hugging Face cache matters —
without it, every rebuild re-downloads ~800 MB of weights.

Open <http://localhost:8000>.

### Local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install                                  # the Prisma CLI, for migrations only

# A database to point at
docker run -d --name laya-pg -p 5432:5432 \
  -e POSTGRES_USER=laya -e POSTGRES_PASSWORD=laya -e POSTGRES_DB=laya_studio \
  postgres:16-alpine

export LAYA_STUDIO_DATABASE_URL=postgresql://laya:laya@localhost:5432/laya_studio
make db-migrate                              # apply the schema

python -m app.cli check                      # Laya runtime (downloads nothing)
python -m app.cli db-check                   # database reachable and migrated

python -m uvicorn app.main:app --reload
```

### Before anything else

```bash
python -m app.cli db-check
```

```json
{
  "database_url": "postgresql://laya:***@localhost:5432/laya_studio",
  "reachable": true,
  "migrated": true,
  "tables": {"evaluations": true, "eval_schemas": true, "...": true},
  "problems": []
}
```

If `migrated` is `false`, the schema has not been applied and the application will
refuse to start — deliberately, because it does not own the schema:

```
The database is reachable but has not been migrated.
Missing table(s): ab_tests, action_logs, evaluations, ...
Apply them with:
    make db-migrate
```

### On migrations

The PostgreSQL schema is owned by [`prisma/schema.prisma`](../prisma/schema.prisma).
The Python application queries it through SQLAlchemy and never creates tables.

```bash
make db-migrate        # apply the committed migrations
make db-migrate-dev    # author a migration from schema.prisma changes
make db-status         # what has been applied
make db-studio         # browse the data
```

A schema change is **two edits** — `prisma/schema.prisma` and `app/models.py`.
`tests/test_schema_drift.py` migrates a scratch database with Prisma and fails if
the two disagree, so you will find out immediately. Full detail in
[data-model.md](data-model.md) and [ADR 0012](adr/0012-prisma-owns-the-schema.md).

## 2. The panel comes up immediately; Laya takes a bit longer

Open <http://localhost:8000> right away — it answers within a second. The engine
loads on a background thread.

Measured with the real runtime on an M-series laptop, warm cache:

```
t= 0.6s   port open        status="starting" engine="loading"
t= 0.6s   GET /            200   with a "Laya is still loading" banner
t= 0.6s   GET /api/ready    503   Retry-After: 10
t= 0.6s   POST /evaluate    503   Retry-After: 10
t=42.1s   engine ready      device=mps checkpoints=["english", "multilingual"]
t=42.9s   POST /evaluate    200   billing @ 0.947
```

So while it loads:

* **every page works** — playground, schemas, workflows, analytics, review;
* the sidebar and a banner say Laya is still loading, and disappear on their own;
* anything that needs a decision returns **503 with a `Retry-After` header**.

On a **cold cache** the engine step is much longer, because the checkpoints
download first (~800 MB for the English and multilingual pair). Mount a Hugging
Face cache — as `docker compose` does by default — so this happens once rather than
on every rebuild.

Nothing to wait for manually. Check on it whenever you like:

```bash
curl -s localhost:8000/api/health | python3 -m json.tool
```

| `engine.state` | Meaning |
|---|---|
| `loading` | Still downloading or loading. Come back in a minute. |
| `ready` | Usable. `loaded_checkpoints` lists what is resident. |
| `failed` | The runtime could not be built. The error is in `engine.error`, and every page shows it. |

If it says `failed`, run `python -m app.cli check` — it verifies the runtime without
downloading anything.

## 3. Put real numbers on the dashboard

A fresh install has the four built-in schema **templates** and no history, so the
dashboard is correctly empty. Load the bundled labelled dataset:

```bash
python -m app.cli load-sample
```

```json
{
  "job_id": 1,
  "schema": "email-triage",
  "filename": "email-triage-sample.csv",
  "rows": 40,
  "accuracy": 0.775,
  "ece": 0.21,
  "band_counts": {"auto": 0, "verify": 9, "review": 31},
  "prediction_distribution": {"billing": 13, "technical": 12, "sales": 6, "other": 9}
}
```

40 rows of test inputs with ground-truth labels, evaluated by the **real** model
through the same code path the Bulk page uses. Nothing is fabricated — you can
open it at `/bulk`, chart it at `/analytics`, re-run it, or compare two schemas
against it at `/ab`.

> `band_counts` showing **0 in the auto band** is the honest first result and worth
> sitting with. Laya's confidence is entropy-based, so a 4-way `choice` rarely
> reaches 0.90. Do not "fix" this by lowering the threshold before you have looked
> at the ECE. [analytics-and-calibration.md](analytics-and-calibration.md) walks
> through it.

## 4. Run your first evaluation

Go to **Playground** (`/`).

1. Click **email sample** to drop a state into the box, or switch the box to
   **Fields** and build a structured state one key at a time. Free text that
   happens to be a JSON object is still detected and sent as a structured state.
2. Pick a schema. `Email triage` is a good first one: it exercises all three
   primitives in one pass.
3. Click **Run evaluation**.

You get a card per question:

* for `choice`, the winning label and the full ranked distribution;
* for `score`, the expected level on the rubric **and** the most likely level;
* for `noul`, the calibrated `P(true)`;
* an **act / verify / escalate** chip derived from the live thresholds;
* a `raw` or `T=…` chip saying whether Laya Studio applied a calibration temperature;
* Laya's own `act …` chip, from its trained act/escalate head.

Watch the numbers. A clear billing email typically returns a top probability around
0.94 with a **confidence around 0.80** — the gap is the entropy measure, and it is
why the band chip may say `verify` when you expected `act`.

### Watch the router work

Paste this and run it:

```
同じ月に二回請求されています。返金してください。
```

The routing line reports `multilingual` and explains why. Script detection runs
before the forward pass, because an English checkpoint fails *loudly but
confidently* on non-Latin input — the published sweep has Khmer at 0.000 accuracy
with 0.952 confidence. Confidence gating alone gives no warning; the script check
does.

## 5. Close the loop

The interesting part. Go to **Review queue** (`/review`).

The 31 escalations from the sample dataset are here. Open one and click
**Correct** with a different label. Do that for a few. Now go to
**Analytics → Calibration monitor → Refit**.

You have just:

1. produced labelled ground truth from a human;
2. fitted a temperature on it;
3. changed what the *next* evaluation reports as its confidence.

That loop — evaluate, escalate, correct, refit — is the point of the system. On
the sample data Laya comes out *under*-confident (it says 53% and is right 80% of
the time), so the fitted temperature sharpens rather than softens, and states that
were previously stuck in `verify` start reaching `auto`.

[analytics-and-calibration.md](analytics-and-calibration.md) explains the
statistics; [workflows.md](workflows.md) explains how to automate it.

## 6. Run your first workflow

Go to **Workflows** (`/workflows`).

Under **Run a workflow**, pick *Email triage (demo)* and paste a state, then click
**Run workflow**. You get the route, the resolved answer, any verification step, and
every action the run took.

| Paste this | What you should see |
|---|---|
| `We were billed twice, please refund the duplicate invoice charge today.` | A confident answer; `auto` if calibration has pushed it over the threshold. |
| `Uh, not sure what this is about honestly` | Low confidence → `review`, and an item appears in the queue. |
| Anything, then **Dry run** | The route the ladder *would* pick, executing no actions. |

Because no LLM provider is configured by default, an uncertain item escalates to a
human rather than auto-executing — the safe default discussed in
[workflows.md](workflows.md#the-rule-that-matters).

## 7. Add the generative fallback

Optional, and the thing that makes the `verify` band do something other than
escalate:

```bash
export LAYA_STUDIO_LLM_PROVIDER=openai
export LAYA_STUDIO_OPENAI_API_KEY=sk-...
```

or `anthropic` with `LAYA_STUDIO_ANTHROPIC_API_KEY`. With a provider configured,
items in the 60–90% band get adjudicated instead of going straight to a human —
and if that call fails, they still go to a human.

## Next steps

| I want to… | Read |
|---|---|
| Understand what Laya actually returns | [laya-integration.md](laya-integration.md) |
| Understand the code | [architecture.md](architecture.md) |
| Integrate over HTTP | [api-reference.md](api-reference.md) |
| Build a real workflow with webhooks | [workflows.md](workflows.md) |
| Deploy this properly | [deployment.md](deployment.md) |
| Know what is unfinished | [ROADMAP.md](ROADMAP.md) |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Panel refuses to start, `The 'laya' package is not importable` | Run `python -m app.cli check`. Install with `pip install -r requirements.txt`. There is no fallback engine by design. |
| `engine.state` stuck on `loading` for a long time | Cold cache: the checkpoints are downloading. Mount `HF_CACHE` so it happens once. `docker compose logs -f` shows the progress. |
| Every page shows "Laya could not be loaded" | The runtime is broken. The exact error is in the banner and on `/api/health`. Run `python -m app.cli check`. |
| Integration gets 503s | The engine is not ready yet. Respect the `Retry-After` header, or route on `/api/ready`. |
| Every item lands in the review queue | No LLM provider configured, so the middle band has nothing to verify with and escalates — the safe default. It is also the honest result before calibration. Set `LAYA_STUDIO_LLM_PROVIDER`, or set thresholds from measured ECE. |
| Almost nothing reaches the `auto` band | Expected for multi-option `choice`: confidence is entropy-based and rarely exceeds 0.9. Calibrate, or lower the threshold knowingly. |
| First evaluation takes ~2 seconds | It should not — the model is primed at startup. If you set `LAYA_STUDIO_LAYA_PRELOAD=false`, the first call pays the load *and* the warm-up. |
| `ValueError: options exceed head_max_len` | A question's options do not fit the sequence budget. The API returns this as a 422 with a `remedy`. Raise `LAYA_STUDIO_LAYA_MAX_LEN` *and* `LAYA_STUDIO_LAYA_HEAD_MAX_LEN` together, reduce options, or shortlist. |
| Slow when switching languages | `LAYA_STUDIO_LAYA_PRELOAD=false` with Laya's default `max_loaded=1` reloads a checkpoint per switch. Turn preload back on. |
| Analytics is empty | No evaluations in the window. Run `python -m app.cli load-sample`. |
| Calibration says *not enough labelled samples* | Fitting needs `LAYA_STUDIO_CALIBRATION_MIN_SAMPLES` (default 30) labelled rows per question. Label in the playground, or work the review queue. |
| Pages look unstyled | Tailwind loads from a CDN; an air-gapped deployment needs it vendored. See [deployment.md](deployment.md#air-gapped-deployments). |
