# 0006 — Analytics aggregates in Python, not SQL

**Status:** Accepted

## Status note (added later)

**The dialect rationale below is historical.** Laya Studio is PostgreSQL-only now
and SQLite has been removed ([ADR 0012](0012-prisma-owns-the-schema.md)), so
"awkward *differently* per dialect" no longer applies — there is one dialect.

The decision still stands, on the two surviving reasons: the metrics are
unit-testable without a database, and there is one implementation to read rather
than one query per metric. But pushing the aggregates into SQL is now a much more
attractive option than it was, and it is the natural fix for the scale ceiling
described below.

## Context

The analytics dashboard needs, over a time window:

* counts by route, source and engine;
* a confidence histogram;
* mean/median/p95 of confidence and latency;
* ECE over labelled rows, plus a per-day trend;
* a distribution of `routing.model`.

Several of these are awkward in SQL and awkward *differently* per dialect:

| Need | SQLite | PostgreSQL |
|---|---|---|
| Bucket by hour/day | `strftime('%Y-%m-%d %H', ts)` | `date_trunc('hour', ts)` |
| Extract `routing.model` from JSON | `json_extract(routing, '$.model')` | `routing->>'model'` |
| Percentile | No built-in | `percentile_cont` |
| Histogram bins | Manual `CASE` chain | `width_bucket` |

Supporting both would mean either a dialect branch in every query or a "PostgreSQL
only" analytics page.

## Decision

Fetch the rows for the window and aggregate in Python
(`services/analytics.py`).

The window is bounded by `MAX_ANALYTICS_ROWS = 50_000`, and the query selects
`Evaluation` objects in ascending `created_at` order so the time-series bucketing
is a single pass.

## Consequences

**Good.**

* **Identical behaviour on both backends**, which matters because SQLite is the
  documented default and PostgreSQL is the documented production path. There is no
  page that works on one and not the other.
* **The metrics are testable without a database** in principle, and against the
  against a test double trivially in practice. The whole `test_api_analytics.py`
  suite seeds six evaluations and asserts real numbers.
* One implementation to read. `timeseries` is a `dict` of buckets and a loop, not
  a `CASE`-chained window function.
* Adding a metric is editing one Python function.

**Bad.**

* **It does not scale.** 50,000 rows fetched per dashboard load is a lot of
  objects. The cap is a guard rail, not a solution — above it, the numbers are
  silently computed from a truncated window.
* Memory is proportional to the window, not to the result.
* Every analytics request pays full deserialisation cost, including the JSON
  columns it does not need.

**This is the known ceiling.** It is the trade named in
[ROADMAP.md](../ROADMAP.md#4-analytics-aggregates-in-python).

## When to revisit

The trigger is concrete: when a deployment's 7-day window exceeds ~50,000
evaluations, or when a dashboard load takes more than about a second.

The migration path is incremental and does not require changing the API:

1. Push the aggregates that *are* portable into SQL first — counts by route,
   source and engine need no dialect branch at all.
2. Keep Python for the histogram and percentiles, which are the genuinely awkward
   ones.
3. If PostgreSQL-only analytics becomes acceptable, `width_bucket` and
   `percentile_cont` collapse most of the remaining work.
4. Failing all that, a materialised `daily_metrics` table refreshed on a schedule
   removes the problem entirely.

## Alternatives considered

**SQL with a dialect branch behind a `DialectAdapter`.** Supports both properly at
full scale. Rejected for now because it is roughly three times the code for two
backends, and the honest scale limit is documented rather than hidden.

**PostgreSQL only; drop SQLite.** *This was rejected at the time and has since been
done anyway*, for migration tooling rather than for analytics. See
[ADR 0012](0012-prisma-owns-the-schema.md). The dialect-neutrality argument in this
ADR is consequently obsolete.

**Precompute on write (increment counters in the evaluation path).** Scales
perfectly and makes the dashboard trivially fast. Rejected because it couples the
hot path to every dashboard metric and makes historical recomputation impossible —
adding a metric would only work from that point forward.
