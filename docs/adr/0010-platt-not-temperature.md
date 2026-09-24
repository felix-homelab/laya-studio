# 0010 — Calibration is a two-parameter logistic map, not a single temperature

**Status:** Accepted

## Context

The calibration monitor was built on temperature scaling, matching the remedy the
Laya model card describes:

```
p' = sigmoid( logit(p) / T )
```

One parameter, fitted by minimising binary NLL of top-1 correctness, over a
log-spaced sweep of `T ∈ [0.05, 20]`. The model card's own framing — mean ECE
falling from 0.466 to 0.081 after "a temperature refit" — is what this mirrors.

Then the engine was run for real, and the fit did this:

```
fitted                             department T=19.357068671982873 n=40
```

`19.36` is the edge of the search range. The fit had run as far as it could.

Investigating produced a hard result rather than a tuning problem:

```
raw mean confidence  0.404
observed accuracy    0.850
```

Every confidence in the sample sits below 0.5 while most outcomes are correct.
`apply_temperature(0.404, 19.36) = 0.495` — the fit dragged the value *toward* 0.5,
away from the truth it was supposed to move toward.

The reason is structural. For any `T > 0`, `sigmoid(logit(p) / T)` keeps `p` on the
same side of 0.5:

| `T` | `p'` for `p = 0.404` |
|---|---|
| 0.1 | 0.020 |
| 0.5 | 0.315 |
| 1.0 | 0.404 |
| 2.0 | 0.452 |
| 20.0 | 0.495 |

The one-parameter family can only steepen or flatten a curve **around the
midpoint**. It cannot express "roughly 0.40 really means roughly 0.85", and its
best available answer is worse than doing nothing.

This is not specific to Laya, but it is *systematic* for Laya: its `confidence` is a
normalised Shannon entropy, `1 - H(p)/log(k)`, whose neutral point is not 0.5 in
the way a calibrated probability's is.

## Decision

Fit a two-parameter logistic map — Platt scaling:

```
p' = sigmoid(slope · logit(p) + intercept)
```

The intercept is what moves the decision boundary; this is the whole fix.

Constraints and guards, each answering a failure that was observed:

1. **`slope > 0`, always.** The map must be rank-preserving: it must never reorder
   the options inside a `choice` breakdown. Enforced by rejecting a fit whose slope
   or whose score/outcome correlation is non-positive.
2. **Scores are winsorised to `logit ∈ [-6, 6]`** before fitting. Laya clamps its
   probabilities to 1e-6, so a confident answer can arrive with a logit near 14; one
   such row has enough leverage to make the normal equations numerically singular
   and the slope diverges (measured: 10⁵).
3. **A ridge penalty on the slope only**, so near-separable samples cannot produce
   an arbitrarily steep map, while the intercept stays unbiased.
4. **Damped Newton (IRLS with step-halving).** A full step overshoots badly on
   nearly separable data. The objective is convex, so damping makes it converge.
5. **The fit must beat the identity on NLL.** If it does not, no recalibration is
   applied and the response says so.
6. **A slope below `0.05` is replaced by a base-rate map** (`slope = 0`,
   `intercept = logit(base_rate)`). Such a slope is a step function, and a
   calibrated output of 0 or 1 for every row destroys the breakdown.
7. **Zero spread in the scores means a base-rate map**, not a fit. With every
   confidence identical the slope is unidentifiable and IRLS walks off to a huge
   value (measured: slope 62). Mapping every row to the observed frequency is the
   correct calibration when the score carries no information.

## Consequences

**Good.**

* **It works.** Measured on the bundled 40-row sample against real Laya:

  | | ECE | MCE | Brier | stated vs observed |
  |---|---|---|---|---|
  | before | 0.4462 | 0.7519 | 0.3550 | 0.404 vs 0.850 |
  | after | **0.0544** | 0.1207 | 0.1140 | 0.845 vs 0.850 |

  And the number that matters operationally: rows reaching a 0.90 auto threshold
  went from **2 to 15**. Calibration is what makes the confidence ladder usable
  rather than permanently stuck in the middle band.

* Three degeneracies that a single temperature silently mishandled now produce an
  explicit, explained outcome instead: constant scores, separable samples, and
  anti-correlated confidence.

* The `ActiveCalibration` response carries `slope` and `intercept` rather than a
  bare `T`, so a reader can see what the map does at any point.

**Bad.**

* Two parameters need more labelled rows than one to fit *stably*. Below roughly
  30 the ridge dominates and the map stays near the identity — which is the right
  conservative behaviour, but it means small samples get little correction.

* `CalibrationSnapshot` gained `slope` and `intercept` columns, and `temperature`
  became nullable (it is undefined for a base-rate map). At the time there was no
  migration tooling, so this needed `ALTER TABLE` by hand — which is part of why
  [ADR 0012](0012-prisma-owns-the-schema.md) moved the schema under Prisma. The
  equivalent change now is an edit to `prisma/schema.prisma` and
  `make db-migrate-dev`.

* More code: IRLS with damping, winsorisation, a ridge, and four guard branches,
  against a ~40-line grid search.

**Neutral.**

* `apply_temperature` remains in `calibration.py` and is still tested. It is a
  meaningful special case (`slope = 1/T`, `intercept = 0`) and the test that it
  *cannot* fix an understated curve is what documents this ADR's premise.

## Alternatives considered

**Keep one temperature and accept it fails on Laya.** Rejected: it demonstrably
made the calibration worse than the identity, and the run-to-the-range-boundary
behaviour would have been invisible in the UI.

**Clamp the temperature range and move on.** Rejected: the problem is the family,
not the range. A narrower range would have made the damage subtler, not smaller.

**Bin the confidences and use the empirical accuracy per bin (histogram
binning).** Simple, non-parametric, and it can express any monotone relationship.
Rejected because it needs far more data than a panel accumulates to avoid
overfitting — 30 rows across 10 bins is three rows per bin — and it is not
rank-preserving by construction (adjacent bins can invert), which would visibly
scramble a `choice` breakdown.

**Isotonic regression.** Rank-preserving and non-parametric, so it fits the shape
of the problem well. Rejected for now because it overfits badly below a few hundred
points and there is no natural shrinkage. Worth revisiting once deployments have
labelled history — and the guard structure above (objective improvement, bounded
output) would carry over unchanged.

**Drop the calibration feature and only *report* ECE.** The honest fallback, and
strictly better than a calibration that makes things worse. Rejected because the
report alone leaves the operator to do the correction by hand, and the two-stage
story — Laya corrects the distribution, Laya Studio corrects the confidence — is a
real and useful one.

**Rescale to the base rate first, then apply one temperature** (a fixed
recentring followed by a one-parameter fit). Equivalent to a constrained version of
the chosen design, with the recentring fixed rather than fitted. Rejected because
the correct recentring is exactly what is unknown, and fitting it is the point.
