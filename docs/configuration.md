# Configuration

Every setting is read from the environment, optionally seeded from a `.env` file
at the repository root. Real environment variables always win over `.env`.

Copy [`.env.example`](../.env.example) to `.env` and edit.

The prefix is `LAYA_STUDIO_` throughout. All values are optional; the defaults
below are what the application uses when a variable is unset.

---

## Application

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_APP_NAME` | `Laya Studio` | Shown in the UI and the OpenAPI title. |
| `LAYA_STUDIO_ENVIRONMENT` | `development` | `development` \| `production` \| `test`. Display only. |
| `LAYA_STUDIO_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. |
| `LAYA_STUDIO_SECRET_KEY` | `change-me-in-production` | Reserved for future session signing. **Change it in production.** |
| `LAYA_STUDIO_CORS_ORIGINS` | *(empty)* | Comma-separated. Empty means same-origin only, which is what the bundled UI needs. Set it only if you are hosting a separate frontend. |
| `LAYA_STUDIO_DATA_DIR` | `<repo>/data` | Upload scratch *and* the log store. The database is on a server, so nothing else durable lives here; it is a directory so a large upload cannot fill the container's writable layer, and in Docker it is bind-mounted (`./data:/app/data`) so the logs are readable from the host. |

## Observability

Every log line, request and traceback is written to a rotated JSONL file and served
read-only from `/api/observability/*`. See
[observability-and-mcp.md](observability-and-mcp.md).

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_LOG_JSONL` | `true` | `false` = stderr only; nothing is written to disk and no directory is created. |
| `LAYA_STUDIO_LOG_DIR` | `<data dir>/logs` | Where the records go. Bind-mounted out of the container by default. |
| `LAYA_STUDIO_LOG_MAX_MB` | `8` | Per file, before it rotates. |
| `LAYA_STUDIO_LOG_BACKUP_COUNT` | `5` | Rotated files kept per process. |
| `LAYA_STUDIO_LOG_RETENTION_DAYS` | `14` | Rotated files older than this are deleted. `0` disables pruning. |
| `LAYA_STUDIO_LOG_REQUESTS` | `true` | Record every HTTP request (method, path, status, duration, correlation id). |
| `LAYA_STUDIO_OBSERVABILITY_TOKEN` | *(unset)* | When set, `/api/observability/*` requires `X-Observability-Token`. |

## MCP

The panel serves its own tool surface at `/mcp`. It can read the logs **and** create,
run and delete the panel's data, so treat it like the ingestion surface rather than
like a page. See [observability-and-mcp.md](observability-and-mcp.md).

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_MCP_ENABLED` | `true` | `false` = the route is not registered at all. |
| `LAYA_STUDIO_MCP_TOKEN` | *(unset)* | When set, `/mcp` requires `Authorization: Bearer <token>` or `X-MCP-Token`. A warning is logged at startup outside `development` while this is unset. |
| `LAYA_STUDIO_MCP_ALLOWED_HOSTS` | *(empty)* | Extra `Host` headers to accept, comma-separated (e.g. `my-panel.local,10.0.0.5`). Loopback and the container name are always accepted; anything else gets a 421. |
| `LAYA_STUDIO_MCP_BASE_URL` | `http://127.0.0.1:8000` | Which panel a **standalone** server (`python -m app.mcp`) talks to. The mounted endpoint ignores it. |
| `LAYA_STUDIO_MCP_TIMEOUT` | `300` | Seconds allowed for one tool call. Generation-length, because a bulk job against a cold engine is slow. |

## Database

**PostgreSQL only.** There is no SQLite path: `normalize_database_url` rejects any
other scheme, so a misconfiguration fails immediately with an explanation rather
than working and then diverging.

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_DATABASE_URL` | `postgresql://laya:laya@localhost:5432/laya_studio` | A standard PostgreSQL DSN. |

### Use the plain `postgresql://` form

`prisma migrate` reads **this same variable** and cannot parse SQLAlchemy's driver
suffix. Write the plain form and the application adds `+psycopg` itself:

```bash
# Correct — both the app and Prisma can read it
LAYA_STUDIO_DATABASE_URL=postgresql://laya:laya@db:5432/laya_studio

# Also accepted by the app, but Prisma will reject it
LAYA_STUDIO_DATABASE_URL=postgresql+psycopg://laya:laya@db:5432/laya_studio
```

`laya-studio db-check` warns if a driver is baked in. `postgres://` and
`postgresql+psycopg2://` are both normalised, so a DSN pasted from a hosting
provider works.

### The schema is not the application's

Tables come from **Prisma's migrations**, not from the models. The application
verifies the schema at startup and refuses to start if it is missing, naming the
command to run. See [data-model.md](data-model.md) and
[ADR 0012](adr/0012-prisma-owns-the-schema.md).

```bash
make db-migrate        # npx prisma migrate deploy   — apply
make db-migrate-dev    # npx prisma migrate dev      — author a change
make db-status         # what has been applied
laya-studio db-check   # connectivity + migrated + row counts
```

### Connection pooling

Defaults are SQLAlchemy's: a queue pool with `pool_pre_ping`, and a 30-second
checkout timeout. Tune via the URL query string when you run many replicas:

```
postgresql://user:pass@host/db?pool_size=10&max_overflow=20
```

## Inference

Laya is the only engine. There is no `LAYA_STUDIO_ENGINE` setting, no fallback,
and no configuration value that selects an alternative: if the runtime is not
importable the panel refuses to start. `python -m app.cli check` diagnoses the
environment without downloading weights.

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_LAYA_PRELOAD` | `true` | Keep `english` and `multilingual` resident. Costs ~2.4 GB of RAM and a slower first boot; saves a multi-second checkpoint reload whenever the language changes. |
| `LAYA_STUDIO_LAYA_DEVICE` | `auto` | `auto` \| `cpu` \| `cuda` \| `mps`. `auto` lets Laya pick (cuda → mps → cpu). Passed to `Router` only if it accepts the keyword. |
| `LAYA_STUDIO_LAYA_CACHE_DIR` | `~/.cache/huggingface` | Sets `HF_HOME` and `HF_HUB_CACHE`. Point it at a mounted volume in Docker or every rebuild re-downloads ~400 MB per checkpoint. |
| `LAYA_STUDIO_LAYA_TOKEN` | — | Hugging Face token, for a gated or private mirror. Falls back to `HF_TOKEN`. |
| `LAYA_STUDIO_LAYA_MAX_LOADED` | Laya's default (`1`) | How many checkpoints stay in memory. Raise it if you serve `typed-decisions` alongside the routed pair. |
| `LAYA_STUDIO_LAYA_MAX_LEN` | checkpoint's own (512 english / 1024 multilingual) | Sequence budget per question. |
| `LAYA_STUDIO_LAYA_HEAD_MAX_LEN` | checkpoint's own (192 english / 256 multilingual) | Budget for the options plus instructions. |
| `LAYA_STUDIO_LAYA_OFFLINE` | `false` | Sets `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE`. For air-gapped installs with a pre-populated cache. |

### Startup is asynchronous

The engine is built on a background thread. The panel is reachable within a second
of the process starting; the engine becomes usable when the checkpoints are
resident. Measured with the real runtime (warm cache): port open at **0.6s**, engine
ready at **42s**.

While it loads:

* every page renders, with a banner saying Laya is still loading;
* `GET /api/health` returns 200 with `status: "starting"`;
* `GET /api/ready` returns 503;
* any endpoint that needs a decision returns **503 with a `Retry-After` header**.

This is why the container healthcheck uses `/api/health` (liveness, 15s start
period) rather than `/api/ready`. If you run behind an orchestrator, use
`/api/ready` as the readiness probe so traffic is only routed once decisions can
actually be served. See [ADR 0011](adr/0011-background-engine-startup.md).

### Preload, and why the default is `true`

`Router` keeps at most `max_loaded` checkpoints resident and evicts
least-recently-used. With the default of 1, a process that alternates languages
reloads a checkpoint mid-request — seconds, against milliseconds for the detection
that picked it. Preloading both auto-routed checkpoints removes that entirely.

`typed-decisions` is not preloaded: only an explicit `model=` request can reach it,
so there is no point holding 421M parameters for it.

Turn preload off if RAM is tight and your traffic is single-language. The cost is
a multi-second stall on the first request in each language.

### The token budgets

`head_max_len` and `max_len` are Laya's, read from each checkpoint's
`rl_agent_config.json`. **Raise both or neither.**

`head_max_len` must stay below `max_len`. Laya truncates options to fit the head
budget and then truncates the whole sequence to `max_len`; if the head budget is
too large for the sequence, the trailing options fall off the end and Laya raises.
Measured on laya 0.3.5, 40 long options against `max_len=512`: it works at the
default 192 and **fails** at 700.

Laya Studio therefore refuses a `head_max_len` that is not below `max_len`,
logging the reason rather than breaking a schema that was working:

```bash
LAYA_STUDIO_LAYA_MAX_LEN=1024
LAYA_STUDIO_LAYA_HEAD_MAX_LEN=512
```

The real fix for a large taxonomy is shortlisting rather than a bigger budget —
see [ROADMAP.md](ROADMAP.md#9-predict_shortlist-for-high-cardinality-choice).
Full mechanics: [laya-integration.md](laya-integration.md#token-budgets).

## Calibration

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_CALIBRATION_ENABLED` | `true` | When false, raw confidences are used and every question is chipped `raw`. |
| `LAYA_STUDIO_CALIBRATION_MIN_SAMPLES` | `30` | Labelled rows required before a temperature is fitted. Below this the evaluator uses the identity. |
| `LAYA_STUDIO_CALIBRATION_CACHE_TTL` | `300` | Seconds to cache a resolved temperature. Prevents one DB round-trip per question per evaluation. |

`MIN_SAMPLES` is a real trade-off. 30 is enough to notice gross miscalibration and
not enough to estimate it precisely — raising it to 100+ gives a more trustworthy
`T`, at the cost of leaving the system uncalibrated for longer.

`MIN_SAMPLES=0` disables the guard entirely and will fit a temperature to two
rows. Useful in tests; not otherwise.

## The confidence ladder

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_AUTO_THRESHOLD` | `0.90` | At or above: execute the automatic actions. |
| `LAYA_STUDIO_VERIFY_THRESHOLD` | `0.60` | At or above: consult the generative fallback. Below: human review. |

Both are clamped to `[0, 1]`, and `VERIFY_THRESHOLD` is forced `<= AUTO_THRESHOLD`
— a verify threshold above the auto threshold would make the middle band
unreachable.

These are the global defaults. Each workflow can override both.

**Do not set these before you have looked at your ECE.** A threshold is only
meaningful against calibrated scores. See
[analytics-and-calibration.md](analytics-and-calibration.md).

## Generative fallback

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_LLM_PROVIDER` | `none` | `none` \| `openai` \| `anthropic`. |
| `LAYA_STUDIO_LLM_TIMEOUT` | `30` | Seconds. Also used as the SMTP timeout. |
| `LAYA_STUDIO_LLM_MAX_TOKENS` | `512` | The verification reply is one small JSON object; this is generous. |
| `LAYA_STUDIO_OPENAI_API_KEY` | *(unset)* | |
| `LAYA_STUDIO_OPENAI_MODEL` | `gpt-4o-mini` | |
| `LAYA_STUDIO_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Override for Azure OpenAI, vLLM, Ollama, LiteLLM, or any OpenAI-compatible gateway. |
| `LAYA_STUDIO_ANTHROPIC_API_KEY` | *(unset)* | |
| `LAYA_STUDIO_ANTHROPIC_MODEL` | `claude-3-5-haiku-latest` | |
| `LAYA_STUDIO_ANTHROPIC_BASE_URL` | `https://api.anthropic.com/v1` | |

`llm_enabled` is true only when the selected provider also has a key. Selecting
`openai` without `OPENAI_API_KEY` logs a startup warning and sends the middle band
straight to review — the safe failure.

### Pointing at a local model

```bash
LAYA_STUDIO_LLM_PROVIDER=openai
LAYA_STUDIO_OPENAI_BASE_URL=http://localhost:11434/v1
LAYA_STUDIO_OPENAI_MODEL=llama3.1:8b
LAYA_STUDIO_OPENAI_API_KEY=ollama   # any non-empty value
```

The provider is a plain `/chat/completions` client, so anything OpenAI-compatible
works.

## Actions

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_WEBHOOK_TIMEOUT` | `10` | Seconds. |
| `LAYA_STUDIO_WEBHOOK_ALLOWED_HOSTS` | *(empty)* | Comma-separated allow-list that **bypasses** the SSRF guard. Empty means no non-public target is reachable. |
| `LAYA_STUDIO_EMAIL_ENABLED` | `false` | Master switch for email steps. |
| `LAYA_STUDIO_SMTP_HOST` | *(unset)* | |
| `LAYA_STUDIO_SMTP_PORT` | `587` | |
| `LAYA_STUDIO_SMTP_USERNAME` | *(unset)* | Omit for an unauthenticated relay. |
| `LAYA_STUDIO_SMTP_PASSWORD` | *(unset)* | |
| `LAYA_STUDIO_SMTP_FROM` | `laya-studio@localhost` | |
| `LAYA_STUDIO_SMTP_STARTTLS` | `true` | |

### The SSRF allow-list, carefully

By default, a webhook step cannot reach any private, loopback, link-local,
reserved, multicast or unspecified address — including `169.254.169.254`, the
cloud metadata service. This is the difference between "a webhook feature" and "an
unauthenticated SSRF primitive", since ingestion can be open.

Additions to `WEBHOOK_ALLOWED_HOSTS` are matched by **hostname**, not IP, and
bypass the address check entirely for that host. Only list hosts you control and
that genuinely need to be reachable:

```bash
LAYA_STUDIO_WEBHOOK_ALLOWED_HOSTS=internal-api.corp.example
```

Never add `169.254.169.254`. The guard exists because letting the decision engine
POST to arbitrary URLs on your internal network is a genuinely bad idea.

## Ingestion

| Variable | Default | Notes |
|---|---|---|
| `LAYA_STUDIO_INGEST_TOKEN` | *(unset)* | When set, `/api/ingest/*` requires a matching `X-Ingest-Token` header (constant-time compared). When unset, ingestion is open. |
| `LAYA_STUDIO_MAX_UPLOAD_MB` | `16` | Multipart upload cap, enforced before parsing. |

`INGEST_TOKEN` is the only authentication in the application, and it covers the
only surface designed to be called by third parties. **Set it before exposing the
panel to anything.** The UI hides the token requirement from no one: the workflows
page states whether a token is needed.

## Precedence

1. Real environment variables
2. `.env` at the repository root
3. The built-in defaults above

`.env` is loaded with `setdefault`, so an exported variable always wins.

## Verifying the effective configuration

```bash
python -m app.cli info
```

```json
{
  "app": "Laya Studio",
  "version": "0.1.0",
  "environment": "development",
  "database_url": "postgresql+psycopg://laya:***@localhost:5432/laya_studio",
  "engine": {
    "name": "laya", "available": true, "device": "mps",
    "preload": true, "max_loaded": 2,
    "loaded_checkpoints": ["english", "multilingual"],
    "head_max_len": null, "startup_seconds": 42.4,
    "configuration": {
      "english": {
        "max_len": 512, "head_max_len": 192,
        "temperature": [1.6369, 1.2514, 1.9834],
        "temperature_by_options": {"choice:2": 1.9064, "choice:3-5": 1.7602,
                                   "choice:6-10": 1.0000, "choice:11+": 0.1006}
      }
    }
  },
  "llm": { "provider": "none", "enabled": false, "model": null },
  "thresholds": { "auto": 0.9, "verify": 0.6 },
  "calibration": { "enabled": true, "min_samples": 30 }
}
```

At runtime, `GET /api/health` returns the same information plus row counts, which
is what the container healthcheck uses.

## Topology caveats

**Thresholds and calibration are process-global.** Changing
`LAYA_STUDIO_AUTO_THRESHOLD` requires a restart. Changing a *workflow's*
thresholds takes effect immediately, because they live in the database.

**Settings are read once.** The `Settings` object is a frozen snapshot built on
first use. Nothing re-reads the environment at runtime; use `reload_settings()`
from a shell if you are scripting. The engine is cached against a key made of the
`LAYA_*` inference settings, so changing one and calling `reload_settings()` builds
a fresh engine rather than reusing a stale one.

**Multiple replicas share a database but not a cache.** Each process keeps its own
resolution cache for the TTL window. With `CALIBRATION_CACHE_TTL=0` every replica
re-derives the temperature, which is correct if slightly wasteful.

## Production checklist

```bash
LAYA_STUDIO_ENVIRONMENT=production
LAYA_STUDIO_SECRET_KEY=$(openssl rand -hex 32)
LAYA_STUDIO_INGEST_TOKEN=$(openssl rand -hex 32)
LAYA_STUDIO_DATABASE_URL=postgresql+psycopg://user:pass@host/db
LAYA_STUDIO_LAYA_CACHE_DIR=/models          # mounted, so restarts skip the download
LAYA_STUDIO_LLM_PROVIDER=openai              # or accept that the middle band escalates
LAYA_STUDIO_OPENAI_API_KEY=sk-...
LAYA_STUDIO_LOG_LEVEL=INFO
```

Run `python -m app.cli check` in the container before declaring it healthy — it
verifies the runtime without downloading anything.

and, if you run behind a proxy, put the panel behind your existing access control
— it has none of its own beyond the ingestion token. See
[deployment.md](deployment.md#hardening).
