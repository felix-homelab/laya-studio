# 0009 — Laya is the only engine

**Status:** Accepted
**Supersedes:** [0002](0002-mock-engine.md)

## Context

The panel originally shipped two implementations of the `DecisionEngine`
contract: the real `LayaEngine`, and a deterministic lexical `MockEngine` that made
the panel bootable, testable and demoable without a GPU, a Hugging Face
round-trip, or roughly 1.7 GB of weights.

The mock was labelled honestly at every level — `Evaluation.engine` recorded
`"mock"`, every playground response carried a "probabilities are simulated, not
calibrated" warning, the sidebar showed a persistent badge, the footer said so, and
`/api/health` reported `is_real: false`. `LAYA_STUDIO_ENGINE=laya` turned the
automatic fallback into a hard error for anyone who wanted it.

It was still the wrong trade.

The problem is not that the panel *could* be run against fake numbers. It is that
it could be run against fake numbers **without the operator noticing**, because the
distinguishing signals were ambient — a badge, a warning, a footer — rather than
load-bearing. An operator who missed them would read a confidence distribution,
set thresholds from it, and act on it. A decision system exists to be trusted; a
decision system that can quietly stop being real has no business shipping.

There was a second, sharper failure. The mock's confidence distribution was
concentrated in the middle band, and real Laya's is too — but for different reasons
(softmax entropy against the entropy-based `confidence` Laya actually reports). So
the panel *looked* like it was behaving correctly while demonstrating the wrong
thing. The seeded demo even produced a fitted temperature of `0.4526` that was
correct for the mock and meaningless for Laya.

## Decision

Remove the mock entirely.

1. **Delete `app/inference/mock_engine.py` and `app/seed.py`.**
2. **Delete `LAYA_STUDIO_ENGINE`.** There is no setting. `build_engine()` returns a
   `LayaEngine`; there is no branch.
3. **Fail fast.** `require_engine()` runs in the lifespan. If the runtime is
   unusable the process logs the reason and exits, rather than serving a UI where
   every action fails.
4. **Remove `is_real`** from the engine protocol. With one engine the field has no
   discrimination to offer and only invited "which one am I running?" reasoning that
   should now be impossible.
5. **Keep the protocol, move the double.** `DecisionEngine` stays abstract;
   `tests/doubles.py` provides `ScriptedEngine` and `LexicalEngine`.
   `app.inference.set_engine` is the only substitution point, it is called from
   nowhere in `app/`, and it is unreachable from configuration and from HTTP.
6. **Add a real integration suite.** `tests/test_real_laya.py` (13 tests, marked
   `real_laya`, deselected by default) is now the executable form of
   `docs/laya-integration.md`.
7. **Replace the sample generator with a sample dataset.** `laya-studio
   load-sample` runs a bundled 40-row labelled CSV through the real engine via the
   ordinary bulk path. It records genuine evaluations of a test set — the same thing
   you would upload yourself — rather than fabricating rows.

## Consequences

**Good.**

* The panel cannot serve a number Laya did not produce. This is now a structural
  property, not a labelling convention.
* The UI lost a badge, a warning and two footer notes, and gained nothing to
  replace them with — which is the point.
* Adapter verification stopped being aspirational. Once a real runtime was
  reachable, the previously-"reviewed only" `laya_engine.py` was exercised and
  **four real bugs surfaced immediately**: `confidence` is entropy-based rather
  than a top probability; `routing["model"]` — not the top-level `model` — names the
  checkpoint; `score` distributions are keyed by index with a separate `legend`;
  and raising `head_max_len` above `max_len` makes Laya *fail* where the default
  succeeded. None of those were findable by reading the model card.
* The test suite got **faster** — 172 tests in ~4 seconds against a lightweight
  double, instead of ~40 seconds.

**Bad.**

* Installation is now heavy: torch plus ~800 MB of weights before the first
  evaluation. There is no "just look at the UI" path.
* The container is ~2.5 GB rather than ~180 MB.
* Anyone running the tests needs `laya` importable if they want the integration
  suite. The fast suite still does not.
* Demo content is thinner: a fresh install has templates and an empty dashboard,
  where a seeded install had charts. `load-sample` restores that with real data,
  but it needs a working engine and a few minutes.

**Neutral.**

* `Evaluation.engine` remains in the schema. It always reads `laya` today, but it
  is the first thing you want to know about a historical row, and it costs one
  column.

## Alternatives considered

**Keep the mock, gate it harder.** For example: refuse to start with it unless
`LAYA_STUDIO_ALLOW_SIMULATED=true` is set explicitly, and stamp every response.
Better than the status quo, and still leaves the failure mode reachable. Rejected
because "it is off unless you turn it on" is exactly the configuration mistake the
decision is trying to make impossible.

**Keep the mock, but generate the demo data with the real engine.** Addresses the
misleading-demo half without addressing the trust half. The sample-dataset command
does this part — but by evaluating a real CSV through the real path, so there is no
second engine involved at all.

**Keep the mock for tests only, in `app/`.** Tests would import it directly instead
of through `set_engine`. Rejected because it keeps a fake engine inside the shipped
package, where it can be imported by application code and quietly used again. Test
doubles belong in `tests/`.

**Require a GPU.** Would have made this decision easier, and is unnecessary: CPU
inference works, it is simply slower. Confirmed at ~34 ms per question on MPS and
usable on CPU.
