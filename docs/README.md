# Laya Studio — documentation

A web panel for **Laya**, Convai Innovations' non-autoregressive "System 1"
decision model. Laya scores typed questions (`choice`, `score`, `noul`) over a text
state and returns probabilities in a single forward pass.

This directory is the full documentation set. If you are new, read it in the order
below.

---

## Reading order

### 1. Get it running

| Document | Read it when |
|---|---|
| [getting-started.md](getting-started.md) | You want a running panel and your first evaluation in five minutes. |
| [configuration.md](configuration.md) | You need to know what every environment variable does. |

### 2. Understand it

| Document | Read it when |
|---|---|
| [laya-integration.md](laya-integration.md) | **Read this early.** The verified runtime contract, and the three properties of Laya that change how you should read every number. |
| [architecture.md](architecture.md) | You want the layers, the request paths, and *why* it is built this way. |
| [data-model.md](data-model.md) | You are querying the database, or adding a feature that needs storage. |
| [workflows.md](workflows.md) | You are building automation on top of the confidence ladder. |
| [analytics-and-calibration.md](analytics-and-calibration.md) | You want to know whether the confidences can be trusted. |

### 3. Operate and extend it

| Document | Read it when |
|---|---|
| [api-reference.md](api-reference.md) | You are integrating over HTTP rather than through the UI. |
| [observability-and-mcp.md](observability-and-mcp.md) | You are debugging a running panel, or wiring an agent to it. |
| [deployment.md](deployment.md) | You are putting this somewhere other than your laptop. |
| [testing.md](testing.md) | You are writing or running tests. |
| [ROADMAP.md](ROADMAP.md) | You want to know what is done, what is next, and what is deliberately out of scope. |
| [adr/](adr/) | You want the reasoning behind a specific structural decision. |

---

## The four feature modules

Each maps to a page, a chunk of the API, and a set of tables.

| Module | Page | API prefix | The question it answers |
|---|---|---|---|
| 1. Decision playground | `/` | `/api/playground` | "What does Laya say about this state, and how sure is it?" |
| 1b. Bulk & A/B testing | `/bulk`, `/ab` | `/api/bulk`, `/api/ab` | "Does it hold up across a dataset, and does my wording change matter?" |
| 2. Workflow automation | `/workflows` | `/api/workflows`, `/api/ingest` | "What should happen automatically, and what needs a human?" |
| 3. Schema & template management | `/schemas` | `/api/schemas` | "How do I define, version and reuse an evaluation?" |
| 4. Analytics & review | `/analytics`, `/review` | `/api/analytics`, `/api/review` | "Are the numbers true, and what is waiting on a person?" |

---

## Three things to know before you trust anything

These are the properties that most often surprise people, and all three were
measured against the installed runtime rather than taken from documentation.

**1. `confidence` is an entropy measure, not a probability of being right.** Laya
computes `1 - H(p) / log(k)` over the temperature-scaled distribution. A 4-way
`choice` with a 0.94 top probability reports a confidence of about 0.80, and a
0.90 threshold rejects it. Every threshold you set interacts with this.
→ [laya-integration.md](laya-integration.md#four-details-that-are-easy-to-get-wrong)

**2. Laya already applies its own temperature scaling**, fitted per question type
and option-count bucket, inside the checkpoint. Every shipped temperature except
one is above 1 — the model card's "over-confident, needs refitting" story, already
corrected for you. The exception, `choice:11+`, ships below Laya's clamp floor, so
the runtime clamps it and tells you confidence from that bucket is uncalibrated.
That is why the schema linter warns at 11 options.
→ [laya-integration.md](laya-integration.md#checkpoint-calibration-is-already-applied)

**3. Laya Studio's calibration is a second stage, not a repeat.** Laya corrects the
*distribution*. Laya Studio measures whether the *reported confidence* predicts
correctness on your rows, and fits a two-parameter logistic map on top. Measured on
the bundled sample it takes ECE from **0.446 to 0.054** — and rows reaching a 0.90
auto threshold from 2 to 15.
→ [analytics-and-calibration.md](analytics-and-calibration.md#the-calibration-map)

And the standing caveat from the model card itself:

> *"Laya is a fast base to specialise, not a zero-shot decision engine."*

The base checkpoints are near-chance on typed decisions. Expect to fine-tune per
domain, and evaluate on your own data — which is what the bulk and A/B modules are
for.

---

## Where things live

```
app/
  config.py          every setting, read from the environment
  models.py          SQLAlchemy models for all four modules
  schemas.py         Pydantic wire contract
  inference/         the Laya adapter, routing, calibration maths
  llm/               OpenAI + Anthropic providers and the verification prompt
  services/          the domain layer (evaluation, bulk, ab, workflows, actions, analytics, review)
  routers/           HTTP surface
  observability/     the JSONL log store, request/exception capture
  mcp/               the MCP tool surface, served at /mcp
  templates/         Jinja2 pages
  static/            vanilla JS + CSS
  samples.py         bundled labelled datasets
datasets/            sample CSVs, evaluated for real
tests/               250 tests; no GPU, no network, no weights
docs/                this directory
```

## Regenerating the API reference

The interactive reference is generated from the code and is always current:

* Swagger UI — <http://localhost:8000/api/docs>
* ReDoc — <http://localhost:8000/api/redoc>
* OpenAPI JSON — <http://localhost:8000/api/openapi.json>

[api-reference.md](api-reference.md) is the hand-written companion: it explains
semantics, ordering and gotchas the schema cannot express.
