# syntax=docker/dockerfile:1

# =============================================================================
# Laya Studio
#
# One image. Two hard dependencies, neither optional:
#
#   * Laya — the only engine; a panel without it cannot evaluate anything.
#   * PostgreSQL — the only database. The schema is owned by Prisma's migrations
#     (prisma/schema.prisma), applied by the `migrate` service in
#     docker-compose.yml. This image carries no JavaScript runtime, so it never
#     runs migrations itself; it verifies the schema and refuses to start if the
#     migrations have not been applied.
#
# The weights are NOT baked in. They are ~400 MB for the English checkpoint and
# ~1.6 GB for all three; mount a Hugging Face cache at /models and they are
# fetched once instead of on every rebuild:
#
#   docker run -v ~/.cache/huggingface:/models -p 8000:8000 laya-studio
#
# For CUDA, override the torch index:
#   docker build --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 .
# =============================================================================

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TOKENIZERS_PARALLELISM=false

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# --- dependencies ------------------------------------------------------------
# torch first, from the wheel index we actually want, then pinned as a constraint
# for the rest of the install. Without the pin, a `laya` torch requirement that
# build does not satisfy would make the resolver quietly swap it for a PyPI
# wheel; with it, the build fails here instead.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --index-url "${TORCH_INDEX_URL}" torch \
    && pip freeze | grep -i '^torch==' > /tmp/torch-constraint.txt \
    && pip install -r requirements.txt --constraint /tmp/torch-constraint.txt \
    && rm /tmp/torch-constraint.txt

# --- application -------------------------------------------------------------
COPY pyproject.toml README.md ./
COPY app ./app
# Bundled sample datasets, used by `laya-studio load-sample`.
COPY datasets ./datasets

# Writable locations. /app/data holds upload scratch and the JSONL log store — the
# database is on a server. Mount something over it: docker-compose.yml binds it to
# ./data on the host, so the logs can be read without `docker exec` and a large
# upload lands on the host disk. Started without a mount, both go to the
# container's writable layer.
ENV HF_HOME=/models \
    LAYA_STUDIO_DATA_DIR=/app/data \
    LAYA_STUDIO_ENVIRONMENT=production \
    LAYA_STUDIO_LAYA_CACHE_DIR=/models

# No DATABASE_URL default is baked in: with no SQLite fallback, the DSN is
# deployment configuration and belongs to the compose file or the orchestrator.
# There is a development default in app/config.py (localhost), so a container
# started without one fails with a connection error against its own loopback —
# which is loud and immediate rather than a subtle misroute.

# This ownership only covers the image's own copies. A bind mount over /app/data
# or /models keeps the *host* owner, so on a Linux host give those directories to
# uid 10001 before the first start (Docker creates missing ones as root):
#
#   mkdir -p data models && sudo chown -R 10001:10001 data models
#
# See docs/deployment.md#persistence. Docker Desktop handles this for you.
RUN mkdir -p /app/data /models \
    && useradd --create-home --uid 10001 laya \
    && chown -R laya:laya /app /models

USER laya

EXPOSE 8000

# Liveness. /api/health returns 200 as soon as the process is up — including
# while Laya is still downloading — because the panel really is serving by then.
# It reports "starting" in its status field until the engine is usable.
#
# For a *readiness* gate (route traffic only once decisions can be served), use
# /api/ready, which returns 503 until the checkpoints are resident:
#
#   HEALTHCHECK CMD curl -fsS http://localhost:8000/api/ready || exit 1
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
