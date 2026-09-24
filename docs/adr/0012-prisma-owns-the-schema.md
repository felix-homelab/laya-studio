# 0012 — PostgreSQL only, and Prisma owns the schema

**Status:** Accepted

## Context

The application started on SQLite: a file, zero setup, and `docker compose up` that
worked without a second service. It supported PostgreSQL as an option
(`LAYA_STUDIO_DATABASE_URL`), and everything was written to stay dialect-neutral —
which is why analytics aggregates in Python
([ADR 0006](0006-analytics-in-python.md)) rather than emitting `strftime` on one
backend and `date_trunc` on the other.

Two things made that the wrong place to be:

1. **No migrations.** Tables came from `Base.metadata.create_all()` on startup. Any
   change to a column meant `ALTER TABLE` by hand, or a database rebuild. Fine while
   the model was settling; not fine once someone had a review queue they cared
   about. The [roadmap](../ROADMAP.md) listed Alembic as the fix.

2. **Two backends, neither exercised properly.** The suite ran on SQLite while
   production was documented as PostgreSQL. Every dialect difference was a latent
   production-only bug, and the guard that would have caught it — running the tests
   against the real thing — did not exist.

The stated goal was to manage migrations with **Prisma**. That is a Node tool with
its own schema language: it cannot read SQLAlchemy models, and the application
cannot query through it without adopting `prisma-client-py`.

## Decision

**PostgreSQL is the only database, and `prisma/schema.prisma` is the source of
truth for DDL.**

Concretely:

1. **SQLite is removed.** `normalize_database_url` *rejects* any non-Postgres
   scheme, and `create_engine` has no SQLite branch. This is a property of the code,
   not a convention — a misconfiguration fails at once with an explanation rather
   than working-and-then-diverging.

2. **`psycopg[binary]` is a required dependency.** Not an extra.

3. **Prisma owns every table.** `prisma/schema.prisma` defines all twelve;
   `prisma migrate` applies them. There is a `migrate` service in
   `docker-compose.yml` that runs `prisma migrate deploy` and exits, and the app
   waits for it — in its own image, because migrations need Node and the application
   image deliberately does not carry a JavaScript runtime.

4. **The application never creates a schema.** `init_db()` is gone.
   `verify_schema()` runs at startup: if any expected table is missing the process
   refuses to start and names the exact command to run. `create_schema()` still
   exists, is documented as test-only, and is used only by the suite.

5. **SQLAlchemy stays the query layer.** `app/models.py` is unchanged in structure
   and every service keeps its existing queries.

6. **A drift test guards the seam.** `tests/test_schema_drift.py` creates a scratch
   database, runs `prisma migrate deploy` against it, and compares the result —
   every table, column, type, nullability, primary key, unique constraint, foreign
   key and index name — against `Base.metadata`. It fails with a list of exact
   differences.

7. **The schema was aligned to Prisma's PostgreSQL defaults** where they differed:
   `String(n)` became `Text` (PostgreSQL gives `varchar(n)` no advantage, and
   Prisma maps `String` to `text`), `JSON` became `JSONB`, and `Float` became
   `Double` so the DDL text matches rather than being coincidentally equivalent.

## Consequences

**Good.**

* **Migrations exist**, are versioned, are reviewable SQL, and are applied in a
  container before the app starts. `prisma migrate dev` authors them; `prisma
  migrate deploy` applies them; `prisma migrate status` shows where you are.
* **The suite runs on the real backend.** All 192 tests, against a throwaway
  `postgres:16` container started by the session. The class of bug that only
  appears in production is gone.
* **The dialect-neutral contortions can go.** The Python-side analytics aggregation
  is now a style choice rather than a constraint — SQL aggregation is a much more
  attractive option than it was ([ADR 0006](0006-analytics-in-python.md) is updated
  to say so).
* **Timestamps are correct.** `@db.Timestamptz(3)` gives real `timestamptz`; the
  SQLite column stored a naive string and relied on a helper to re-attach UTC.

**Bad.**

* **Two definitions of one schema.** This is the real cost, and it is why the drift
  test exists. `prisma/schema.prisma` and `app/models.py` must both be updated for a
  schema change. Forget the second and the tests catch it; forget the first and
  `prisma migrate` produces nothing.
* **A Node toolchain in the development loop.** Authoring a migration needs
  `npm install` and `npx`. The runtime image does not, but contributors do.
* **One more service.** `docker compose up` now starts a database, a one-shot
  migration container and the app, and the app waits on the first two. More moving
  parts, and a `docker compose up app` in isolation will fail.
* **The test suite needs Docker** (or `LAYA_STUDIO_TEST_DATABASE_URL`). It went from
  4.6s to ~14s, and it can no longer run in an environment without a container
  runtime or an external Postgres.
* **SQLite's zero-setup `docker compose up` is gone.** The first run is heavier.

**Neutral.**

* `String(n)` length limits are no longer enforced by the column type. They were
  never meaningfully enforced — PostgreSQL does not pad or validate — but the
  metadata is now silent about them, so `services/decision_schemas.py` and the
  request schemas are the only place a length is checked. They do check.

## Alternatives considered

**Alembic instead of Prisma.** Alembic generates migrations from the SQLAlchemy
models, so there is exactly one definition of the schema and drift is impossible.
That is a genuinely better fit for this codebase, and it was offered as the
recommended option. Rejected by the project owner, who wants Prisma specifically —
a legitimate reason when the tooling has to fit an existing workflow.

**Adopt `prisma-client-py` and drop SQLAlchemy.** Removes the drift entirely: one
schema, one query layer. Rejected because it means rewriting `models.py`, `db.py`
and all ten service modules (~2,500 lines) onto a client that is markedly less
mature for JSONB columns, complex filters and transactions — and the analytics and
bulk services lean on all three. The seam is worth a drift test.

**Keep SQLite and maintain both dialects.** Rejected: the suite only ever exercised
one of them, so "supports PostgreSQL" was a claim nothing verified.

**Keep SQLite as a development convenience only.** Rejected because the point of
moving is that development and production behave identically. A development-only
dialect is the same unverified claim with extra code.

**Run `prisma migrate deploy` inside the application entrypoint.** Would avoid the
extra service, at the cost of shipping Node in the runtime image (~180 MB) and an
application that mutates its own schema at startup — which is exactly the
responsibility we are trying to hand to a migration tool.
