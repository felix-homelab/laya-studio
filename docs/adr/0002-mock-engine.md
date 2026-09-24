# 0002 — A deterministic mock engine ships in the default image

**Status:** Superseded by [0009](0009-laya-is-the-only-engine.md)

> **Superseded.** The mock engine was removed. It made the panel explorable
> without a multi-gigabyte download, and it was labelled honestly everywhere — but
> the trade was still wrong. A decision system whose output *can* be numbers no
> model produced teaches its operator to distrust the wrong thing. Laya is now the
> only engine and the process refuses to start without it.
>
> Kept because the alternatives considered below are still the alternatives, and
> because "we thought about this and changed our minds" is more useful to a reader
> than a clean slate.

## Context

Laya is real inference code: torch, transformers, and roughly 1.7 GB of weights
across three checkpoints. A panel that requires all of that before it will render
a page is a panel nobody evaluates.

But a panel whose numbers are fabricated is worse than one that does not start —
it teaches the operator to trust something meaningless.

## Decision

Ship `MockEngine`, a deterministic lexical scorer implementing the same
`DecisionEngine` contract, and select it automatically when the real runtime is
unavailable (`LAYA_STUDIO_ENGINE=auto`, the default).

Then pay the honesty cost in full. A simulated result is marked at **every** level:

* `Evaluation.engine == "mock"` and `engine_model` recorded per row;
* a warning on every playground response;
* a persistent "Simulated engine" badge in the sidebar;
* a footer note on every page;
* `describe()["note"]` returned from `/api/health`;
* the README's second section, and a "Three things to know" block at the top of
  the docs index.

The mock itself is built to be *informative* when it is wrong, not plausible:
no lexical evidence produces a near-uniform distribution, which correctly lands in
the review queue rather than faking a confident answer.

## Consequences

**Good.**

* `docker compose up` is a few seconds and a ~180 MB image.
* The test suite is hermetic: 169 tests, no GPU, no network, no model download,
  and deterministic assertions. This is the largest single consequence.
* Every downstream feature — visualizer, bulk metrics, workflows, calibration,
  review loop — is exercisable from day one.

**Bad.**

* A careless deployment can serve simulated probabilities. Mitigated by
  `LAYA_STUDIO_ENGINE=laya` (hard error instead of fallback) and by the badge, but
  it is a real footgun.
* The mock's numbers are *wrong in a specific way* — its softmax is under-confident,
  the opposite of real Laya. Anyone drawing conclusions about calibration from the
  demo is drawing the wrong ones. Documented in
  [analytics-and-calibration.md](../analytics-and-calibration.md#a-worked-example-with-real-numbers).
* It is additional code to maintain, with its own behaviour to reason about.

## Alternatives considered

**Ship no fallback; fail loudly without Laya.** Honest, and arguably correct for a
production tool. Rejected because it makes the project unevaluable — you cannot
decide whether the panel is worth the download without seeing the panel.

**A recorded fixture set: run Laya once, replay the JSON.** Better numbers, no
runtime cost. Rejected because it cannot answer arbitrary states, which is the
entire point of a playground.

**Generate plausible-looking random numbers.** Rejected outright. Non-deterministic
(the test suite would flake) and actively deceptive (it would look like a real
model responding). The chosen design is deterministic *and* visibly degrades on
inputs it cannot match.
