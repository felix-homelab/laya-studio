# Laya Studio

A web panel for **[Laya](https://huggingface.co/convaiinnovations/laya)** — Convai
Innovations' non-autoregressive "System 1" decision model. Laya answers *typed
questions* (`choice`, `score`, `noul`) about a text state and returns calibrated
probabilities in a single forward pass. It never generates text, so it cannot
hallucinate a JSON payload or invent an enum value.

Laya Studio wraps that engine in the tooling a decision system needs once it
leaves a notebook: a playground for building schemas, batch and A/B evaluation,
a hybrid confidence-gated automation engine, and a calibration monitor that tells
you whether the stated confidences are actually true.

```bash
docker compose up --build     # http://localhost:8000
```

**→ Start at [docs/README.md](docs/README.md).** New here? Read
[docs/getting-started.md](docs/getting-started.md) (five minutes), then
[docs/architecture.md](docs/architecture.md).

---

## What it does

| Module | What you get |
|---|---|
| **Playground** | Type a state (free text, an email, or arbitrary JSON), pick a schema, get a per-question probability breakdown with an act-vs-escalate indicator and Laya's own `act_probability`. Live linting as you edit the questions. |
| **Bulk testing** | Upload CSV/JSON, score every row, and get accuracy, mean confidence, ECE and a routing-band breakdown next to the predictions. |
| **A/B testing** | Run one dataset through two immutable schema revisions. Separates real improvement from probability noise via flip rate, net gain and per-label transitions. |
| **Workflows** | Build the automation as a script: a trigger block, then colour-coded action blocks stacked on each rung of the confidence ladder (webhook, email, review queue, log note). Drag to reorder, pick placeholder tokens by clicking, no JSON. Every action is logged. |
| **Analytics** | Confidence distribution, volume and escalation over time, workflow route mix, a reliability diagram over labelled rows, and the checkpoint's own temperature table. |
| **Migrations** | PostgreSQL DDL is defined in `prisma/schema.prisma` and applied with `prisma migrate`. A drift test keeps it in step with the SQLAlchemy models. |
| **Review queue** | Where low-confidence items land. Approving or correcting writes ground truth back onto the evaluation — which is what feeds calibration. |
| **Observability & MCP** | Every log line, request and traceback is written to a rotated JSONL file and served read-only from `/api/observability/*`. The panel also publishes its own tool surface over MCP at `/mcp` — 60 tools — so an agent can drive it *and* read the logs of what it just did. See [docs/observability-and-mcp.md](docs/observability-and-mcp.md). |

## The confidence ladder

```
confidence >= 0.90   →  auto    execute the automatic actions
confidence >= 0.60   →  verify  ask a generative model to adjudicate,
                                then auto-execute or escalate
otherwise            →  review  park it in the human queue
```

Two behaviours worth knowing before you trust it:

* **An unavailable fallback is not a green light.** If no LLM is configured, or the
  call fails, or the model returns something unparseable, the item escalates to
  human review. It never auto-executes on an unverified middle band.
* **The thresholds are per workflow**, defaulting to
  `LAYA_STUDIO_AUTO_THRESHOLD` / `LAYA_STUDIO_VERIFY_THRESHOLD` — and you should
  set them from measured ECE, not from intuition. See
  [why](docs/analytics-and-calibration.md#why-this-is-not-optional).

## Requirements

Two hard dependencies, neither optional:

* **Laya** — the only engine, so the panel cannot serve a decision without it. Needs
  Python 3.10+ and pulls torch and transformers. The first run downloads ~800 MB of
  checkpoint weights from Hugging Face; mount a cache so it happens once.
* **PostgreSQL** — the only database. There is no SQLite path, and the schema is
  owned by Prisma's migrations rather than by the application.

```bash
# Docker: db → migrate → app, weights cached on the host
HF_CACHE=~/.cache/huggingface docker compose up --build

# Local
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
npm install                                  # Prisma CLI, for migrations only
export LAYA_STUDIO_DATABASE_URL=postgresql://laya:laya@localhost:5432/laya_studio
make db-migrate                              # apply the schema
python -m app.cli check                      # verify the runtime; downloads no weights
python -m uvicorn app.main:app --reload
```

Once it is up, the panel tells you what it is doing — and so does the panel's own
log store:

```bash
make logs      # the last 20 records: requests, logs, tracebacks
make errors    # distinct exceptions, grouped by raise site
```

The panel answers within a second; the engine loads in the background and says so.
On a warm cache that is ~42s, on a cold one it is the download as well. Anything
that needs a decision returns `503` with a `Retry-After` until it is ready — the UI
banner and `/api/health` both tell you where it is. Use `/api/ready` to gate traffic
on the engine rather than the process.

Verify before you go further:

```bash
python -m app.cli check    # imports, device, cache location (downloads nothing)
python -m app.cli info     # live engine: device, loaded checkpoints, temperature table
```

## Getting something on the dashboard

A fresh install has the four built-in schema **templates** but no history, so the
dashboard is correctly empty. To put real numbers in it, evaluate a real dataset:

```bash
python -m app.cli load-sample
```

That runs the bundled 40-row labelled CSV through the **real** engine via the same
path the Bulk page uses, then prints accuracy, ECE and the routing mix. Nothing is
fabricated: it is an ordinary bulk job you can inspect at `/bulk`, chart at
`/analytics`, and re-run.

```json
{
  "rows": 40,
  "accuracy": 0.85,
  "ece": 0.4462,
  "band_counts": {"auto": 2, "verify": 12, "review": 26},
  "prediction_distribution": {"billing": 13, "technical": 14, "sales": 8, "other": 5}
}
```

Then open `/analytics` to see the spread, and `/review` to work the escalations.

## Important: read this before trusting a number

Three properties of Laya shape everything the panel shows. All three are measured,
not assumed — see [docs/laya-integration.md](docs/laya-integration.md).

**1. `confidence` is not the top probability.** Laya computes it as a normalised
Shannon entropy, `1 - H(p) / log(k)`. A 4-way `choice` with a 0.94 top probability
reports a confidence of ~0.80. The ladder gates on confidence, so this matters. It
also means a 4-way question rarely reaches a 0.90 threshold at all.

**2. Laya already temperature-scales its distributions** — per question type and
option-count bucket, inside the checkpoint. You are not looking at raw logits. One
of those buckets ships out of range and is clamped, which is why the schema linter
warns at **11 options** rather than at some guessed accuracy cliff.

**3. The base checkpoints are near-chance zero-shot.** This is the model's own
position: *"Laya is a fast base to specialise, not a zero-shot decision engine."*
The published 0.766 accuracy figure belongs to a checkpoint fine-tuned on that
benchmark's training split. Expect to fine-tune and calibrate per domain.

Laya Studio's calibration is a **second stage** on top of Laya's: it measures
whether the reported confidence predicts correctness on *your* data, and fits a
two-parameter logistic map on top. Measured on the bundled sample, that takes ECE
from **0.446 to 0.054** — and the number of rows reaching a 0.90 auto threshold from
2 to 15. That is what the review queue and the calibration monitor exist to feed.

## Documentation

| Document | Contents |
|---|---|
| [docs/README.md](docs/README.md) | Index and reading order |
| [docs/getting-started.md](docs/getting-started.md) | Install, first evaluation, first workflow |
| [docs/architecture.md](docs/architecture.md) | Layers, request paths, design decisions |
| [docs/laya-integration.md](docs/laya-integration.md) | The verified runtime contract, routing, token budgets, latency |
| [docs/data-model.md](docs/data-model.md) | Every table and what it is for |
| [docs/workflows.md](docs/workflows.md) | The ladder, actions, writing your own steps |
| [docs/analytics-and-calibration.md](docs/analytics-and-calibration.md) | ECE, temperature scaling, and how to act on the numbers |
| [docs/configuration.md](docs/configuration.md) | Every environment variable |
| [docs/api-reference.md](docs/api-reference.md) | The JSON API, endpoint by endpoint |
| [docs/observability-and-mcp.md](docs/observability-and-mcp.md) | The log store, the log API, and the MCP tool surface |
| [docs/deployment.md](docs/deployment.md) | Docker, PostgreSQL, hardening |
| [docs/testing.md](docs/testing.md) | Running and writing the test suite |
| [docs/ROADMAP.md](docs/ROADMAP.md) | What is built, what is next, what is out of scope |
| [docs/adr/](docs/adr/) | Architecture decision records |

## Project layout

```
app/
  main.py            application factory + lifespan (seeds templates, requires Laya)
  config.py          every setting, read from the environment
  models.py          SQLAlchemy models for all four feature modules
  schemas.py         Pydantic wire contract
  inference/         the Laya adapter, routing, and calibration maths
    laya_engine.py   the only engine
    routing.py       script detection, for the no-forward-pass probe and as a fallback
    calibration.py   pure ECE / temperature-fitting maths
  llm/               OpenAI + Anthropic providers and the verification prompt
  services/          the domain layer: evaluation, bulk, ab, workflows, actions, analytics, review
  routers/           HTTP: playground, schemas, bulk, ab, workflows, ingest, analytics, review, observability
  observability/     the JSONL log store, request/exception capture, its HTTP surface
  mcp/               the MCP server mounted at /mcp, and the tools it exposes
  templates/         Jinja2 pages (Tailwind via CDN)
  static/            vanilla JS + CSS
  samples.py         bundled labelled datasets, evaluated for real
datasets/            the CSVs themselves
prisma/              schema.prisma + migrations — the source of truth for DDL
tests/               237 tests, none of which need a GPU, a network or weights
docs/                everything above, plus the roadmap and ADRs
```

## Tests

```bash
make test                      # starts its own throwaway Postgres, no weights needed
pytest --real-laya tests/      # adds the real-checkpoint integration suite
```

The fast suite needs no GPU, no network and no model download. The engine is
substituted through `app.inference.set_engine`, which is test-only and called from
nowhere in `app/` — there is no configuration setting that can select an
alternative engine, and the panel refuses to start if Laya is unusable. See
[docs/testing.md](docs/testing.md).

## License

Apache-2.0, matching Laya's own license. Laya Studio is an independent panel and is
not affiliated with Convai Innovations.
