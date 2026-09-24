# 0004 — Schema revisions are immutable

**Status:** Accepted

## Context

A "schema" is a named bundle of typed questions. Users edit them constantly —
tweaking instructions, rewording label descriptions, adding an option.

Two features depend on knowing *which version* of a schema produced a given
result:

* **A/B testing.** The entire point is comparing two versions of the same schema.
  If editing a schema mutates it in place, an A/B test cannot express "revision A".
* **Historical evaluation.** A three-month-old evaluation was produced by the
  taxonomy that existed then. Re-running it against today's taxonomy answers a
  different question.

The obvious implementation — a `definition` column on the schema, updated in place —
supports neither.

## Decision

Separate the mutable identity (`EvalSchema`) from immutable snapshots
(`SchemaRevision`).

* `EvalSchema` holds the name, description, tags and a `current_revision` pointer.
* `SchemaRevision` holds a frozen `definition` and a `note`, unique per
  `(schema_id, revision)`.
* Editing metadata (name, description, tags) does **not** mint a revision.
* Editing `questions` **does**, incrementing `current_revision`.
* `Evaluation` stores `schema_revision_id` *and* its own copy of `questions` as
  sent.

## Consequences

**Good.**

* A/B testing compares two real revisions by id, and cannot compare a version
  against itself by accident.
* An evaluation is replayable against its own taxonomy forever, even if the schema
  is deleted — because the question bundle is copied onto the row, not referenced.
* Revisions double as a change log: the `note` field records *why*, and the
  revisions endpoint lists them newest-first.

**Bad.**

* Storage duplication: the question bundle is stored once per revision and once
  per evaluation. For a 4-question schema this is a few hundred bytes per row —
  acceptable, and the price of replayability.
* A "rename plus reword" edit needs the caller to think about whether a revision
  should be minted. The rule is mechanical, so this is learnable rather than
  confusing.
* `current_revision` is denormalised state that must be kept consistent with the
  revisions table. It is only ever written through `update_schema`, which
  maintains both.

## Alternatives considered

**Mutable `definition` plus a separate audit log.** Cheaper on storage. Rejected
because reading a historical decision would require replaying the audit log to
reconstruct the taxonomy at that point — a reconstruction that is easy to get
wrong and impossible to verify.

**Content-addressed revisions (hash of the definition).** Elegant — identical
questions share a revision automatically. Rejected because revision numbers are
what a human types into an A/B form, and `r7` is far more legible than
`a3f8c1e2`.

**Git-style branches.** Far more power than needed. Nobody is asking to merge two
taxonomies.
