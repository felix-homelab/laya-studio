# 0001 — The engine contract is a tolerant normaliser over a dict

**Status:** Accepted — verified against `laya 0.3.5` (see [laya-integration.md](../laya-integration.md))

## Context

Laya returns a nested dict:

```python
result["answers"]["department"]["choice"]
result["answers"]["department"]["confidence"]
result["routing"]["model"]
```

The shape has changed across releases — most notably the field name for the
per-option probability distribution, which has appeared as `probabilities`,
`distribution`, `options` and `scores`, sometimes as a mapping and sometimes as a
list of `{label, probability}` objects. The `choice` key, the `confidence` key and
the `routing` payload have all moved.

Two approaches were available:

1. **Bind tightly.** Declare a Pydantic model matching the current result schema
   and validate against it.
2. **Normalise tolerantly.** Probe for known field names and coerce into a
   stable internal shape.

## Decision

Normalise tolerantly. `inference/base.py` exposes `extract_choice`,
`extract_confidence`, `extract_noul`, `extract_score` and `extract_probabilities`,
each of which probes several plausible field names and accepts either container
shape. `normalize_result` wraps any engine payload into a `DecisionResult`.

`DecisionResult` is the *only* shape the rest of the system sees. Nothing above
`inference/` knows Laya's raw keys.

## Consequences

**Good.**

* A minor Laya release that renames a field degrades into a missing value rather
  than a 500.
* A test double and the real engine are genuinely interchangeable, because the
  contract is a behaviour, not a class. (`tests/doubles.py`.)
* Supporting another model is implementing three methods.

**Bad.**

* A typo'd or genuinely absent field fails silently as `None` rather than loudly at
  validation time. Mitigated by the linter and by the fact that a `None` confidence
  routes to human review rather than auto-executing.
* `extract_probabilities` is ~50 lines of probing that a `Field(alias=...)` would
  express in one.

**Neutral.**

* Recovering a `score` from a distribution (when `score` is absent but per-level
  probabilities are present) is possible as a side effect of the same design.

## What running it for real changed

The tolerance paid for itself, and not in the way expected. Once the adapter ran
against real weights, **none** of the anticipated problems appeared — but four
unanticipated ones did, and three of them were silent wrong values rather than
errors:

* `confidence` is not the top probability. Laya reports a normalised Shannon
  entropy, `1 - H(p)/log(k)`. The tolerant normaliser picked the value up
  correctly and would have been "right" while every threshold and document built on
  top of it was wrong.
* `routing["model"]` names the checkpoint; the top-level `model` key is
  `"laya-rl-agent"`. Probing both, with the *specific* one preferred, was the
  difference between recording `english` and recording an internal class name.
* `score` probabilities are keyed by index (`"0"`, `"1"`) with the level text in a
  separate `legend`. A breakdown that ignored the legend would render bare numbers
  instead of the rubric the author wrote.
* Raising `head_max_len` above `max_len` makes Laya fail where the default
  succeeded — the opposite of the intuitive fix.

The first two are the argument for this ADR. A strict binding to the documented
result schema would have raised a validation error on neither, and would have
stored confident nonsense either way.

## Alternatives considered

**A typed Pydantic model per version, with a version detector.** Would fail loudly
on a mismatch, which is genuinely better for debugging. Rejected because the
failure mode it produces — the panel refuses to process any decision until someone
patches a model — is worse for a *panel* than the one it replaces. A decision
system that degrades to human review is useful; one that refuses to run is not.

Revisit if Laya publishes a stable, versioned result schema.
