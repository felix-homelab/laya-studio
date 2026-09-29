# 0005 — Calibration maths is separate from calibration fitting

**Status:** Accepted

## Context

Calibration has two halves that look like one feature:

* **Maths:** given a list of `(confidence, was_correct)` pairs, compute ECE, MCE,
  the Brier score, a reliability diagram, and fit a temperature.
* **Operations:** decide *whether* there is enough labelled evidence to trust a
  fit, resolve which temperature applies to which surface right now, cache it, and
  persist snapshots for the dashboard.

The first is pure and stateless. The second is entirely about the database and the
process lifecycle.

Where does each belong? The obvious answer — put both in `services/` — would pull
`numpy`-style statistics into the layer that owns DB sessions, and would put the
only interesting algorithms in the project behind a database fixture.

## Decision

Split them, and place the boundary at the layer rule.

* `inference/calibration.py` — **pure, dependency-free maths.** No imports from
  `models`, `db`, or `services`. `calibration_report`, `reliability_bins`,
  `fit_temperature`, `apply_temperature`, `apply_temperature_to_distribution`.
  (The fitter is now `fit_platt`; see [0010](0010-platt-not-temperature.md). The
  layer split below is unchanged.)
* `services/calibration.py` — **stateful operational logic.** Queries labelled
  evaluations, applies the `min_samples` guard, caches per `(schema, question)` with
  a TTL, persists `CalibrationSnapshot` rows, and exposes `ActiveCalibration`.

The direction of dependency is one-way: `services` imports `inference`, never the
reverse. This is the same rule that keeps `inference/` free of database access
generally (see [architecture.md](../architecture.md#layers)).

## Consequences

**Good.**

* The statistics are unit-testable with no fixtures at all:

  ```python
  def test_overconfidence_shows_up_as_a_positive_gap():
      report = calibration_report([0.95] * 100, [True] * 50 + [False] * 50)
      assert report.ece == pytest.approx(0.45, abs=1e-9)
  ```

  Twelve of the calibration tests are like this — hand-computed values, no
  database. They run in milliseconds and they test the actual mathematics.
* No scipy, no numpy. The fitter is plain Python and deterministic — originally a
  log-spaced temperature sweep, now `fit_platt`'s IRLS (see
  [0010](0010-platt-not-temperature.md)).
* The maths is reusable from a notebook or a script with no application context.

**Bad.**

* Two modules named `calibration` in different packages, which is a small
  navigational annoyance. Mitigated by the names saying which is which: `inference`
  owns the maths, `services` owns the fitting.
* `services/calibration.py` must remember to call into the pure layer correctly —
  e.g. clamping `min_samples` behaviour itself rather than in the maths.

## Alternatives considered

**Everything in `services/calibration.py`.** Fewer files. Rejected because it would
make the ECE test require a database, and the ECE calculation is the part most
likely to be silently wrong.

**Everything in `inference/calibration.py`, with the DB passed in.** Keeps one
module. Rejected because it violates the layering rule that `inference/` never
touches persistence, and that rule earns its keep elsewhere.

**Reach for scikit-learn's `CalibratedClassifierCV`.** A well-tested implementation
of exactly this. Rejected because it would add a heavy dependency for one
scalar parameter — and because pulling in sklearn's calibration to fit a
single temperature would obscure what is actually a one-parameter optimisation.
