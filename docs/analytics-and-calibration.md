# Analytics and calibration

The question this page answers: **are the stated confidences true, and does the
confidence gate mean what you think it means?**

## Why this is not optional

Laya's probabilities are temperature-scaled inside the checkpoint, fitted with
RLCD (Reinforcement Learning for Calibrated Decisions) using strictly proper
scoring rules. In principle a stated 0.85 is an 85% chance of being right.

In practice, three things get in the way, and all three are visible in the
running panel.

### 1. `confidence` is an entropy measure, not a probability of being right

Laya computes it as `1 - H(p) / log(k)` over the temperature-scaled distribution.
It is a *measure of how peaked the distribution is*. Whether it equals
`P(correct)` is an empirical claim about your data, and it is exactly what the
calibration monitor tests.

The practical consequence is easy to miss. From a real run:

| Question | Top probability | Reported confidence |
|---|---|---|
| `choice`, 4 options | 0.9432 | 0.7989 |
| `choice`, 4 options | 0.8557 | 0.5898 |
| `choice`, 3 options | 0.7308 | 0.3157 |

The gap widens with the option count. A 4-way question with an overwhelming winner
still reports a confidence that a 0.90 threshold rejects.

### 2. One temperature bucket ships out of range

Laya's English checkpoint carries a temperature for `choice:11+` of **0.1006**,
below its own clamp floor of 0.5. The runtime clamps it and warns:

```
RuntimeWarning: laya: this checkpoint ships temperatures outside [0.5, 5] which
would distort confidence; clamping choice:11+=0.1006. Treat confidence from the
affected buckets as uncalibrated.
```

So an 11-option `choice` question reports a confidence **nobody has calibrated** —
and the confidence ladder gates on exactly that number. The schema linter warns at
11 options for this reason. See
[laya-integration.md](laya-integration.md#checkpoint-calibration-is-already-applied).

### 3. The multilingual checkpoint ships no refit at all

`temperature = [1.0, 1.0, 1.0]` and an empty `temperature_by_options`. Its
probabilities are raw logits. Anything you tuned on `english` does not transfer.

**The net effect**, measured: a five-row labelled batch through real Laya produced
accuracy 0.80 against mean confidence 0.53 — ECE **0.281**, badly under-confident.
The auto band was unreachable; every row landed in verify or review.

That is not a reason to distrust the system. It is the reason the calibration
monitor exists.

---

## The metrics

### ECE — expected calibration error

Bin predictions by stated confidence, then compare each bin's mean confidence
against its observed accuracy. ECE is the sample-weighted average gap:

```
ECE = Σ (|bin| / n) × |accuracy(bin) − mean_confidence(bin)|
```

| ECE | Reading |
|---|---|
| < 0.05 | Well calibrated. Thresholds mean what they say. |
| 0.05 – 0.15 | Moderate. Refit before tightening thresholds. |
| > 0.15 | Badly miscalibrated. Do not gate on these numbers. |

Roughly: *how wrong your confidence is on average*, in probability points.

The report names the direction, because it changes what you do:

> *"Badly under-confident (ECE 0.281: 53.4% stated vs 80.0% observed). Confidence
> gating is unsafe until this is refit."*

Under-confident means the model is better than it claims — your thresholds are
rejecting rows that would have been fine. Over-confident means the opposite, and
your auto band is letting mistakes through.

### MCE — maximum calibration error

The worst single bin's gap. A low ECE with a high MCE means the model is fine on
average but untrustworthy in one region — often the high-confidence tail, which is
where you automated.

### Brier score

Mean squared error of the probabilities. A proper score: lower is better, and it
penalises confident mistakes hardest.

### Reliability diagram

Where all three come from. Each confidence bin shows two bars: **stated confidence**
(blue) and **observed accuracy** (green). Identical bars mean perfect calibration;
the gap is the miscalibration, and the bar's width says how much evidence is behind
it.

```json
{"lower": 0.7, "upper": 0.8, "count": 12, "mean_confidence": 0.744, "accuracy": 0.833, "gap": 0.089}
```

---

## The calibration map

One logistic transform, two parameters:

```
p' = sigmoid( slope · logit(p) + intercept )
```

* **`slope`** steepens or flattens the curve. Fitted positive, always, so the map
  is rank-preserving — it can never reorder the options inside a `choice`
  breakdown.
* **`intercept`** moves the decision boundary. This is the parameter that does most
  of the work.

### Why two parameters and not a single temperature

The obvious choice — and the one the Laya model card's own phrasing suggests — is
one temperature, `p' = sigmoid(logit(p) / T)`. It does not work here, and the
reason is structural rather than a matter of tuning.

For any `T > 0`, a single temperature keeps `p` on the **same side of 0.5**. It can
only steepen or flatten the curve around the midpoint. Measured on the bundled
sample against real Laya:

```
raw mean confidence  0.404
observed accuracy    0.850
```

Every confidence sits below 0.5 while most outcomes are correct. What does a
temperature do?

| `T` | `p'` for `p = 0.404` |
|---|---|
| 0.1 | 0.020 |
| 0.5 | 0.315 |
| 1.0 | 0.404 |
| 2.0 | 0.452 |
| 20.0 | 0.495 |

The best the one-parameter family can manage is 0.495 — it drags the value *toward*
0.5, away from the truth. In the running system this showed up as a fitted
`T = 19.36`, sitting at the edge of the search range because the optimiser had run
as far as it could in a direction that could not help.

The intercept is what lets the map say "roughly 0.40 really means roughly 0.85".
The same data, with two parameters:

| raw | calibrated |
|---|---|
| 0.25 | 0.771 |
| 0.40 | 0.847 |
| 0.60 | 0.909 |
| 0.90 | 0.973 |

This is systematic for Laya specifically: its `confidence` is a normalised Shannon
entropy, `1 - H(p)/log(k)`, whose neutral point is not 0.5 the way a calibrated
probability's is.

Full rationale, including the degeneracies each guard exists for:
[ADR 0010](adr/0010-platt-not-temperature.md).

### Why per-question, not one global map

Miscalibration varies by question type and option count — the same reason Laya
fits its temperatures per `(qtype, option-bucket)`. Laya Studio normalises per
`(schema, question)`:

1. Try the fit for that question.
2. Below `LAYA_STUDIO_CALIBRATION_MIN_SAMPLES` labelled rows (default 30), fall
   back to the schema-wide fit.
3. If that is thin too, use the identity and say so.

Every playground response returns the map that was used, and every question card
carries a `calibrated` chip with the raw value alongside:

```json
{"slope": 0.4277, "intercept": 2.1622, "temperature": 2.338, "source": "fitted", "n_samples": 40,
 "note": "On this sample the reported confidence understates the observed accuracy, so the calibration map raises it (slope 0.428, intercept +2.162)."}
```

`source` is `identity`, `fitted` or `cached`. Fits are cached for
`LAYA_STUDIO_CALIBRATION_CACHE_TTL` seconds. `temperature` is `1 / slope`, shown
for familiarity; it is `null` for a base-rate map.

### When no map is applied

Each of these returns the identity **with an explanation**, rather than a
plausible-looking number:

| Situation | What happens |
|---|---|
| Fewer than `MIN_SAMPLES` labelled rows | Identity; the note names the shortfall. |
| Every outcome identical (all correct or all wrong) | Identity; a scale is not identifiable. |
| Every confidence identical | **Base-rate map** — rows are mapped to the observed frequency, because a constant score genuinely carries no information. |
| The fitted slope would be below 0.05 | Base-rate map. Such a slope is a step function, and a calibrated output of 0 or 1 for every row destroys the breakdown. |
| Confidence is not positively related to correctness | Identity, with a note telling you to investigate the labelling or the question — an anti-correlated confidence is a finding, not something to transform away. |
| The fit does not beat the identity on NLL | Identity; the map was not an improvement. |

Fitting is damped Newton (IRLS with step-halving) on a convex objective, with a
small ridge on the slope and scores winsorised to `logit ∈ [-6, 6]`. No scipy, no
numpy, deterministic.

## A worked example, with real numbers

All of this came from running the app against real Laya on the bundled 40-row
labelled sample. Reproduce the engine side with:

```bash
pytest --real-laya tests/test_real_laya.py -v
```

**Step 1 — measure.** The sample loads through the bulk path:

```
accuracy       0.85
mean conf      0.404
ece            0.4462
bands          {auto: 2, verify: 12, review: 26}
```

**Step 2 — read the direction.** The monitor says *understated*: 40.4% stated
against 85.0% observed. The model is better than it claims, so your thresholds are
rejecting rows that would have been fine.

**Step 3 — refit.**

```bash
curl -X POST localhost:8000/api/analytics/calibration/refit
```

```json
{"snapshots": [{"question": "department", "slope": 0.4277, "intercept": 2.1622,
                "n_samples": 40, "ece": 0.4462, "accuracy": 0.85,
                "mean_confidence": 0.404}],
 "message": "Refit 1 slice(s)."}
```

**Step 4 — the numbers move.**

| | ECE | MCE | Brier | stated vs observed |
|---|---|---|---|---|
| before | 0.4462 | 0.7519 | 0.3550 | 0.404 vs 0.850 |
| after | **0.0544** | 0.1207 | 0.1140 | 0.845 vs 0.850 |

Both rows are a point-in-time measurement of one checkout and one checkpoint:
`python -m app.cli load-sample`, then `POST /api/analytics/calibration/refit`, then
a rerun of the job. Repeating those three steps is what confirms the figures for
*your* data — the exact value a row stores as its calibrated `confidence` is part
of what is being measured, so treat these as the shape of the effect (roughly an
order of magnitude off ECE, in this direction) rather than as constants.

And the reliability diagram becomes a diagram rather than a diagonal line:

```
0.6-0.7  n=  4  stated 0.660  observed 0.750  gap 0.090
0.7-0.8  n=  8  stated 0.746  observed 0.625  gap 0.121
0.8-0.9  n= 13  stated 0.859  observed 0.923  gap 0.064
0.9-1.0  n= 15  stated 0.935  observed 0.933  gap 0.002
```

**Step 5 — the ladder starts working.** This is the payoff, and it is not a
statistical curiosity:

```
rows reaching the 0.90 auto threshold:   2 before  ->  15 after
```

Before calibration, almost nothing could be auto-executed — the confidences were
entropy-based and clustered below the threshold. After, 15 of 40 rows qualify, and
they qualify *legitimately*: their calibrated confidence now predicts roughly 93%
accuracy, which is what a 0.90 threshold is supposed to mean.

That is the loop closing. Measure, correct, and the gate you configured starts
behaving like the gate you meant.

Note the sample size: 40 rows produced a usable map, but 30 is the floor and ECE on
30 rows is a wide interval. A few hundred rows is what a calibration worth relying
on looks like.

## Reading an A/B test

Accuracy alone cannot separate a real improvement from noise.

### Flip rate

The share of rows whose answer changed between revisions. **The headline number
for a wording change**, and it is not "higher is better":

| Flip rate | Reading |
|---|---|
| 0 | The revisions are behaviourally identical on this dataset. Any accuracy difference elsewhere is sampling noise. |
| 1–30% | The change is doing something. Check the transitions for whether it is sensible. |
| > 30% | A lot of churn for a wording tweak. The distinction was probably under-specified — the model was guessing. |

A high flip rate on *unchanged labels* is the signature of an under-specified
question, not of an improvement.

### Net gain

With labels present, the summary splits the flips:

| Field | Meaning |
|---|---|
| `fixed_by_b` | A was wrong, B is right |
| `broken_by_b` | A was right, B is wrong |
| `net_gain` | `fixed_by_b − broken_by_b` |

`accuracy_delta` alone hides a change that fixes 40 rows and breaks 39.

### Transitions

Per-label movement, ordered by frequency, for the rows that actually moved:

```
billing   → technical   18
other     → billing     11
```

18 billing rows sliding into `technical` tells you exactly which distinction you
blurred.

### Confidence delta

`mean_confidence_delta` with counts up and down. If accuracy moved but confidence
did not, your thresholds now sit at a different effective operating point — which
may be the real reason "accuracy improved".

---

## The workflow you should actually follow

**1. Get labelled rows.** Upload a CSV with a `truth` column, or work the review
queue. Without labels there is nothing to measure. `laya-studio load-sample` runs
a bundled 40-row dataset through the real engine if you want a starting point.

**2. Measure before you tune.** Look at accuracy and ECE. Do not be surprised by
either.

**3. Refit if ECE > 0.15.** You cannot tune a threshold against a miscalibrated
score.

**4. Tune the taxonomy with A/B.** One change at a time — instructions, a label
description, the option set. Watch the flip rate. High churn means the question is
under-specified; fix the wording rather than the threshold.

**5. Re-check calibration after any taxonomy change.** A new option set is a new
question surface, and possibly a new temperature bucket. The old fit does not
transfer.

**6. Only then set thresholds.** With ECE < 0.05 a 0.90 threshold means roughly 90%
precision. Before calibration, 0.90 may be unreachable no matter how good the model
is, because the raw confidence is entropy-based. Set the number from the observed
distribution, not from intuition.

**7. Let the review queue feed back.** Corrections are ground truth. Refit on them.

---

## Watching for drift

The monitor reports ECE **per day**, so a trend is visible without refitting.
Rising ECE on a stable question usually means:

* **data drift** — the input distribution moved and the model has not;
* **label drift** — reviewers are applying a different standard than the dataset
  did (check `correction_rate` on the analytics page);
* **taxonomy drift** — someone edited the schema, creating a new surface. This is
  the one the system is built to catch: revisions are immutable, so you can always
  tell which taxonomy produced a given evaluation, and query by
  `schema_revision_id` to isolate before and after.

---

## API

| Endpoint | Returns |
|---|---|
| `GET /api/analytics/dashboard?hours=&bucket=` | Everything below, in one call |
| `GET /api/analytics/overview` | Totals, band counts, rates, latency, top choices |
| `GET /api/analytics/timeseries?bucket=hour\|day` | Volume and escalation per bucket |
| `GET /api/analytics/routes` | Workflow route mix and action outcomes |
| `GET /api/analytics/calibration?hours=` | ECE/MCE/Brier, reliability bins, daily trend, snapshots |
| `GET /api/analytics/calibration/report?schema_id=&question=` | On-demand report for one slice |
| `POST /api/analytics/calibration/refit` | Refit every slice with enough labelled rows; returns `slope` and `intercept` per slice |
| `GET /api/analytics/thresholds` | The live ladder |

Refitting clears the calibration cache, so subsequent evaluations pick up the new map
immediately. It returns `{"snapshots": [], "message": "Nothing to refit: …"}` as a
200 when nothing qualifies — not an error.

---

## Limitations

**Small samples are noisy.** ECE on 30 rows is an estimate with a wide interval.
The report says so below 100 samples and does not claim more precision than it has.
The worked example above fits on 300; five rows produced a number that was correct
but not actionable.

**Top-1 ECE only.** The monitor measures whether the *winning* label's confidence is
honest. It does not verify the rest of the distribution. For a `choice` question
with many plausible options the runner-up probabilities can be wrong even when the
top-1 number is right.

**Per-question, not per-label.** One map is shared across all labels of a
question. A model over-confident specifically on `billing` but well calibrated on
`technical` cannot be corrected by one scalar. Tracked for
[per-label calibration](ROADMAP.md#10-per-label-calibration).

**A base-rate map is not an error, but it is a warning.** When every confidence in
a sample is identical, or the slope collapses, rows map to the observed frequency.
That is the correct calibration — and it also means the confidence is telling you
nothing about which rows are right.

**Stored confidences do not move when you refit.** Each `Evaluation` keeps the
confidence it was created with, plus `raw_confidence`. So a window that spans a
refit contains rows reported on both sides of it, and its ECE is a blend. That is
deliberate — a row should record what it actually said — but it means:

* to see the effect of a refit on a dataset, **re-run it**: the bulk page's
  **Rerun** button re-scores every row and produces calibrated confidences;
* a dashboard built entirely from post-refit traffic is the one that reflects your
  current thresholds. When you change a threshold, expect the bands to shift for
  new rows and stay put for old ones.

**Labels are only as good as their source.** A dataset labelled by an overworked
human produces a well-calibrated model of that human's inconsistency.
`label_source` is recorded for exactly this reason.

**A calibration map cannot fix a broken model.** It rescales confidence; it adds no
information. If accuracy is at the random baseline — which, remember, the *base* Laya
checkpoints are on typed decisions — no map will help. Fine-tune first.

**Small samples produce conservative maps.** The ridge penalty means a 10-row fit
barely moves, which is the right behaviour and also means you will see little
benefit below roughly 30 labelled rows.

**Confidence saturates.** The multilingual checkpoint returned confidence `1.0000`
on a clear non-Latin input. Above the saturation point the map has nothing left to
move, and the ladder cannot separate "very sure" from "certain".
