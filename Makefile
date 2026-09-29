# Laya Studio — developer shortcuts.
#
# `make help` lists everything. The defaults assume a virtualenv on PATH; override
# with e.g. `make run PYTHON=/opt/venv/bin/python`.

PYTHON ?= python3
PORT ?= 8000
HOST ?= 127.0.0.1
COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help venv install install-dev install-js run sample check-engine db-check predict \
        db-migrate db-migrate-dev db-status db-studio db-diff db-up db-reset reset info \
        mcp mcp-http logs errors \
        test test-fast test-real lint fmt check \
        docker-build docker-up docker-down docker-warm docker-migrate docs clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- environment -------------------------------------------------------------

venv: ## Create a virtualenv in .venv
	$(PYTHON) -m venv .venv
	@echo "Activate it with: source .venv/bin/activate"

install: ## Install the runtime (pulls laya, torch, transformers, psycopg)
	$(PYTHON) -m pip install -r requirements.txt

install-dev: ## Install runtime + dev/test dependencies
	$(PYTHON) -m pip install -r requirements.txt pytest ruff

install-js: ## Install the Prisma CLI (needed to author or apply migrations)
	npm install

# --- running -----------------------------------------------------------------

run: ## Run the panel with autoreload (default: http://127.0.0.1:8000)
	$(PYTHON) -m uvicorn app.main:app --reload --host $(HOST) --port $(PORT)

sample: ## Evaluate the bundled labelled dataset with the real engine
	$(PYTHON) -m app.cli load-sample

check-engine: ## Verify the Laya runtime without downloading weights
	$(PYTHON) -m app.cli check

db-check: ## Verify the database connection and that the migrations are applied
	$(PYTHON) -m app.cli db-check

predict: ## Score one state end to end, as a smoke test
	$(PYTHON) -m app.cli predict "We were billed twice and need a refund." --schema email-triage

# --- observability & MCP -----------------------------------------------------

# Both send LAYA_STUDIO_OBSERVABILITY_TOKEN from the environment (export it; the
# Makefile does not read .env). Unset, curl sends no header, which is what a panel
# without a token expects.
logs: ## Print the last records from the running panel's log store (LINES=n)
	@curl -fsS -H "X-Observability-Token: $${LAYA_STUDIO_OBSERVABILITY_TOKEN:-}" \
		"http://$(HOST):$(PORT)/api/observability/logs/tail?lines=$${LINES:-20}&include_traceback=true" \
		| $(PYTHON) -m json.tool

errors: ## List distinct exceptions seen by the running panel, grouped by raise site
	@curl -fsS -H "X-Observability-Token: $${LAYA_STUDIO_OBSERVABILITY_TOKEN:-}" \
		"http://$(HOST):$(PORT)/api/observability/errors" | $(PYTHON) -m json.tool

mcp: ## Serve the MCP tools over stdio, against the running panel (for a desktop client)
	$(PYTHON) -m app.mcp --transport stdio

mcp-http: ## Serve the MCP tools over HTTP on 127.0.0.1:8765
	$(PYTHON) -m app.mcp --transport http --port 8765

# --- database (Prisma owns the schema) ---------------------------------------

db-migrate: ## Apply Prisma's migrations (deploy)
	npx prisma migrate deploy

db-migrate-dev: ## Author a migration from schema.prisma changes (development)
	npx prisma migrate dev

db-status: ## Show which migrations have been applied
	npx prisma migrate status

db-studio: ## Browse the database with Prisma Studio
	npx prisma studio

# Prisma resets the shadow database and replays every migration into it, so it
# must be a throwaway database — never LAYA_STUDIO_DATABASE_URL.
db-diff: ## Print the SQL for schema.prisma changes without applying them (needs LAYA_STUDIO_SHADOW_DATABASE_URL)
	npx prisma migrate diff --from-migrations prisma/migrations --to-schema-datamodel prisma/schema.prisma --shadow-database-url "$${LAYA_STUDIO_SHADOW_DATABASE_URL:?set LAYA_STUDIO_SHADOW_DATABASE_URL to a throwaway database}"

db-up: ## Start just the PostgreSQL service
	$(COMPOSE) up -d db

# Both let `app.cli purge` ask first — it names the database it is about to empty.
# Scripts that really mean it can run `python -m app.cli purge --yes` directly.
db-reset: ## Drop and re-migrate the database (destructive; asks first)
	$(PYTHON) -m app.cli purge
	npx prisma migrate deploy

reset: ## Drop every table, re-apply the migrations, re-seed the templates (destructive; asks first)
	$(PYTHON) -m app.cli purge
	npx prisma migrate deploy
	$(PYTHON) -m app.cli db-check

info: ## Print the effective configuration and the active engine
	$(PYTHON) -m app.cli info

# --- quality -----------------------------------------------------------------

test: ## Run the full test suite
	$(PYTHON) -m pytest tests/

test-fast: ## Run the test suite, stopping at the first failure
	$(PYTHON) -m pytest tests/ -x -q

test-real: ## Run the suite including the real-checkpoint integration tests
	$(PYTHON) -m pytest tests/ --real-laya

lint: ## Static checks (ruff)
	$(PYTHON) -m ruff check app tests

fmt: ## Auto-fix lint findings
	$(PYTHON) -m ruff check --fix app tests

check: lint test ## Lint then test

# --- containers --------------------------------------------------------------

docker-build: ## Build the panel image
	$(COMPOSE) build app

# The host port is compose's LAYA_STUDIO_PORT (default 8000), not PORT, so ask
# compose which one it published.
docker-up: ## Start the panel on http://localhost:8000 (host port: LAYA_STUDIO_PORT)
	$(COMPOSE) up --build -d
	@port=$$($(COMPOSE) port app 8000 | head -n 1 | sed 's/.*://'); \
		echo "Panel: http://localhost:$$port  ·  API docs: http://localhost:$$port/api/docs"

docker-migrate: ## Apply migrations against the running compose database
	$(COMPOSE) run --rm migrate

docker-down: ## Stop the stack and keep volumes
	$(COMPOSE) down

docker-warm: ## Start with the Hugging Face cache mounted, so weights are not re-fetched
	HF_CACHE=$${HF_CACHE:-$$HOME/.cache/huggingface} $(COMPOSE) up --build

# --- housekeeping ------------------------------------------------------------

docs: ## List the documentation
	@ls -1 docs/

clean: ## Remove caches and upload scratch (not the logs, not the database)
	rm -rf .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf data/uploads/*
