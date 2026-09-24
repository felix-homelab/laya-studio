# Architecture decision records

One file per structural decision, in the format: **Context → Decision →
Consequences → Alternatives considered**.

The point is not ceremony. It is that a future reader can tell the difference
between "nobody thought about this" and "this was considered and chosen anyway".

| # | Decision | Status |
|---|---|---|
| [0001](0001-engine-contract.md) | The engine contract is a tolerant normaliser over a dict | Accepted |
| [0002](0002-mock-engine.md) | A deterministic mock engine ships in the default image | **Superseded by 0009** |
| [0003](0003-fallback-fails-closed.md) | An unverifiable middle band escalates rather than auto-executes | Accepted |
| [0004](0004-immutable-schema-revisions.md) | Schema revisions are immutable | Accepted |
| [0005](0005-calibration-math-vs-fitting.md) | Calibration maths is separate from calibration fitting | Accepted |
| [0006](0006-analytics-in-python.md) | Analytics aggregates in Python, not SQL | Accepted |
| [0007](0007-no-build-step-frontend.md) | The frontend has no build step | Accepted |
| [0008](0008-ingest-auth-scope.md) | Ingestion is the only authenticated surface | Accepted |
| [0009](0009-laya-is-the-only-engine.md) | Laya is the only engine | Accepted |
| [0010](0010-platt-not-temperature.md) | Calibration is a two-parameter logistic map, not a single temperature | Accepted |
| [0011](0011-background-engine-startup.md) | The engine is built in the background; the panel serves immediately | Accepted |
| [0012](0012-prisma-owns-the-schema.md) | PostgreSQL only, and Prisma owns the schema | Accepted |
| [0013](0013-log-store-and-mcp-inside-the-panel.md) | The log store is JSONL files, and the MCP server runs inside the panel | Accepted |

## Conventions

**Superseding a decision.** Never edit an accepted ADR to reflect a new reality.
Write a new one and mark the old one `Superseded by 00XX`. The history of *why*
something changed is often more useful than the current answer.

**When to write one.** If a change would be expensive to reverse, surprise a new
contributor, or encode a trade-off someone could reasonably want the other way,
it deserves an ADR. Picking a library does not. Deciding that a failed
verification must fail closed does.

**Length.** Short. Context, decision, consequences, alternatives. If it needs more
than a page, the decision is probably two decisions.
