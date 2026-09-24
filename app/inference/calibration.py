"""Calibration mathematics: reliability bins, ECE/MCE/Brier, temperature scaling.

Laya ships **over-confident** weights — the model card reports mean ECE falling
from 0.466 to 0.081 after a temperature refit. Laya Studio therefore never
presents a raw probability as trustworthy on its own: the analytics dashboard
tracks ECE against observed accuracy, and the evaluator optionally applies a
fitted temperature before persisting a confidence.

Everything in this module is pure and dependency-free so it can be unit-tested
without a database or a model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EPS = 1e-6

#: Temperature search bounds (log-spaced). T > 1 softens an over-confident model.
TEMP_MIN = 0.05
TEMP_MAX = 20.0

#: Bounds on the fitted Platt map, mirroring the temperature range: ``slope =
#: 1 / T``, so T in [0.05, 20] is slope in [0.05, 20]. Beyond these the map is a
#: step function and the calibrated output saturates to 0 or 1 for everything,
#: which would destroy a probability breakdown.
MIN_PLATT_SLOPE = 0.05
MAX_PLATT_SLOPE = 20.0
MAX_PLATT_INTERCEPT = 40.0

#: Logits are winsorised to this range before fitting, which is ``p`` in about
#: [0.0025, 0.9975]. Laya clamps its probabilities to 1e-6, so a confident answer
#: can arrive with a logit near 14 — a single such row has enough leverage to make
#: the IRLS normal equations numerically singular, and the slope then diverges
#: (measured: slope 10^5 on the bundled sample). Winsorising bounds the leverage
#: without changing the fit for any realistic score.
MAX_FIT_LOGIT = 6.0


def _clamp_probability(value: float) -> float:
    return max(EPS, min(1.0 - EPS, float(value)))


def safe_logit(probability: float) -> float:
    """``log(p / (1-p))`` with the boundaries clamped."""
    p = _clamp_probability(probability)
    return math.log(p / (1.0 - p))


def sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponent = math.exp(value)
    return exponent / (1.0 + exponent)


def apply_temperature(confidence: float, temperature: float) -> float:
    """Rescale a single confidence using ``p' = sigmoid(logit(p) / T)``.

    ``T > 1`` pulls an over-confident score toward 0.5; ``T < 1`` sharpens.
    """
    if temperature <= 0 or temperature == 1.0:
        return _clamp_probability(confidence)
    return _clamp_probability(sigmoid(safe_logit(confidence) / temperature))


def apply_temperature_to_distribution(
    distribution: dict[str, float], temperature: float
) -> dict[str, float]:
    """Rescale a full choice/score distribution, preserving the simplex.

    Operates on ``log p`` so the transformation is the multiclass analogue of
    :func:`apply_temperature`, then renormalises.
    """
    if not distribution:
        return {}
    if temperature <= 0 or temperature == 1.0:
        return {label: float(value) for label, value in distribution.items()}

    powered: dict[str, float] = {}
    for label, probability in distribution.items():
        p = _clamp_probability(probability)
        powered[label] = math.exp(math.log(p) / temperature)

    total = sum(powered.values())
    if total <= 0:  # pragma: no cover - guarded by clamping above
        return dict(distribution)
    return {label: value / total for label, value in powered.items()}


@dataclass(slots=True)
class ReliabilityBin:
    """One column of the reliability diagram."""

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float
    gap: float

    def to_dict(self) -> dict[str, float | int]:
        return {
            "lower": round(self.lower, 4),
            "upper": round(self.upper, 4),
            "count": self.count,
            "mean_confidence": round(self.mean_confidence, 6),
            "accuracy": round(self.accuracy, 6),
            "gap": round(self.gap, 6),
        }


@dataclass(slots=True)
class CalibrationReport:
    """Summary of how well stated probabilities match observed accuracy."""

    n_samples: int = 0
    accuracy: float | None = None
    mean_confidence: float | None = None
    ece: float | None = None
    mce: float | None = None
    brier: float | None = None
    bins: list[ReliabilityBin] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "accuracy": self.accuracy,
            "mean_confidence": self.mean_confidence,
            "ece": self.ece,
            "mce": self.mce,
            "brier": self.brier,
            "bins": [item.to_dict() for item in self.bins],
            "note": self.note,
        }


def reliability_bins(
    confidences: list[float],
    correct: list[bool | int],
    n_bins: int = 10,
) -> list[ReliabilityBin]:
    """Group (confidence, correctness) pairs into equal-width confidence bins."""
    if n_bins < 1:
        raise ValueError("n_bins must be >= 1")
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have the same length")

    width = 1.0 / n_bins
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(n_bins)]

    for confidence, outcome in zip(confidences, correct, strict=True):
        value = _clamp_probability(confidence)
        index = min(int(value / width), n_bins - 1)
        buckets[index].append((value, 1.0 if outcome else 0.0))

    bins: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        lower = index * width
        upper = lower + width
        if not bucket:
            bins.append(
                ReliabilityBin(
                    lower=lower,
                    upper=upper,
                    count=0,
                    mean_confidence=0.0,
                    accuracy=0.0,
                    gap=0.0,
                )
            )
            continue
        count = len(bucket)
        mean_confidence = sum(item[0] for item in bucket) / count
        accuracy = sum(item[1] for item in bucket) / count
        bins.append(
            ReliabilityBin(
                lower=lower,
                upper=upper,
                count=count,
                mean_confidence=mean_confidence,
                accuracy=accuracy,
                gap=abs(accuracy - mean_confidence),
            )
        )
    return bins


def calibration_report(
    confidences: list[float],
    correct: list[bool | int],
    n_bins: int = 10,
) -> CalibrationReport:
    """Compute ECE, MCE, Brier score and the reliability diagram."""
    if not confidences:
        return CalibrationReport(note="No labelled samples available yet.")

    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have the same length")

    outcomes = [1.0 if item else 0.0 for item in correct]
    total = len(confidences)

    bins = reliability_bins(confidences, outcomes, n_bins=n_bins)
    # Population-weighted mean of the per-bin gaps — the standard ECE definition.
    ece = sum((item.count / total) * item.gap for item in bins if item.count)
    mce = max((item.gap for item in bins if item.count), default=0.0)
    brier = sum(
        (_clamp_probability(confidence) - outcome) ** 2
        for confidence, outcome in zip(confidences, outcomes, strict=True)
    ) / total

    accuracy = sum(outcomes) / total
    mean_confidence = sum(_clamp_probability(item) for item in confidences) / total

    note = ""
    if total < 30:
        note = (
            f"Only {total} labelled samples; ECE estimates are noisy below ~100 rows."
        )

    return CalibrationReport(
        n_samples=total,
        accuracy=accuracy,
        mean_confidence=mean_confidence,
        ece=ece,
        mce=mce,
        brier=brier,
        bins=bins,
        note=note,
    )


def _binary_nll(temperature: float, logits: list[float], targets: list[float]) -> float:
    total = 0.0
    for logit, target in zip(logits, targets, strict=True):
        probability = _clamp_probability(sigmoid(logit / temperature))
        total -= target * math.log(probability) + (1.0 - target) * math.log(1.0 - probability)
    return total / len(logits)


def _platt_nll(slope: float, intercept: float, logits: list[float], targets: list[float]) -> float:
    total = 0.0
    for x, target in zip(logits, targets, strict=True):
        probability = _clamp_probability(sigmoid(slope * x + intercept))
        total -= target * math.log(probability) + (1.0 - target) * math.log(1.0 - probability)
    return total / len(logits)


def _correlation(left: list[float], right: list[float]) -> float:
    """Pearson correlation. Zero when either series has no spread."""
    n = len(left)
    mean_left = sum(left) / n
    mean_right = sum(right) / n
    covariance = sum(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True)
    )
    var_left = sum((a - mean_left) ** 2 for a in left)
    var_right = sum((b - mean_right) ** 2 for b in right)
    denominator = math.sqrt(var_left * var_right)
    return 0.0 if denominator < 1e-12 else covariance / denominator


def _golden_section_minimize(function, low: float, high: float, iterations: int = 80) -> float:
    """1-D minimisation by golden-section search (no scipy dependency)."""
    inverse_phi = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = low, high
    c = b - inverse_phi * (b - a)
    d = a + inverse_phi * (b - a)
    fc, fd = function(c), function(d)

    for _ in range(iterations):
        if b - a < 1e-6:
            break
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - inverse_phi * (b - a)
            fc = function(c)
        else:
            a, c, fc = c, d, fd
            d = a + inverse_phi * (b - a)
            fd = function(d)
    return (a + b) / 2.0


@dataclass(slots=True)
class PlattFit:
    """A monotone logistic recalibration of a reported confidence.

        p' = sigmoid(slope * logit(p) + intercept)

    **Why two parameters rather than one temperature.** A single temperature,
    ``sigmoid(logit(p) / T)``, keeps ``p`` on the same side of 0.5 for every
    positive ``T``: it can only steepen or flatten a curve around the midpoint. That
    is useless for the case Laya actually presents. Measured on the bundled 40-row
    sample, the mean reported confidence was 0.404 against an observed accuracy of
    0.85 — the whole curve sits below 0.5 while most outcomes are correct. No
    temperature can express "roughly 0.40 really means roughly 0.85"; the best the
    one-parameter family can do is run to the edge of its range and drag everything
    toward 0.5, which is *worse* than doing nothing.

    The intercept is what lets the map move the decision boundary. The slope is
    constrained positive so the mapping stays rank-preserving: it must never
    reorder the options within a distribution.
    """

    slope: float = 1.0
    intercept: float = 0.0
    fitted: bool = False
    note: str = ""

    @property
    def is_identity(self) -> bool:
        return abs(self.slope - 1.0) < 1e-9 and abs(self.intercept) < 1e-9

    @property
    def temperature(self) -> float | None:
        """The equivalent single temperature, for display. ``T = 1 / slope``.

        ``None`` when the slope is zero, which happens for a base-rate map — there
        is no temperature that flattens a curve *and* re-centres it.
        """
        if abs(self.slope) < 1e-12:
            return None
        return 1.0 / self.slope

    def apply(self, confidence: float) -> float:
        return _clamp_probability(sigmoid(self.slope * safe_logit(confidence) + self.intercept))

    def describe(self) -> str:
        if not self.fitted:
            return self.note or "Identity mapping."
        if self.intercept > 0.05:
            direction = "understates"
            effect = "raises it"
        elif self.intercept < -0.05:
            direction = "overstates"
            effect = "lowers it"
        else:
            direction = "roughly matches"
            effect = "leaves it about where it is"
        return (
            f"On this sample the reported confidence {direction} the observed accuracy, "
            f"so the calibration map {effect} "
            f"(slope {self.slope:.3f}, intercept {self.intercept:+.3f})."
        )


#: Platt's target smoothing, which stops the fit saturating on small samples.
def _smooth_targets(targets: list[float]) -> list[float]:
    positives = sum(targets)
    negatives = len(targets) - positives
    high = (positives + 1.0) / (positives + 2.0)
    low = 1.0 / (negatives + 2.0)
    return [high if target else low for target in targets]


def fit_platt(
    confidences: list[float],
    correct: list[bool | int],
    *,
    iterations: int = 60,
    tolerance: float = 1e-10,
    ridge: float | None = None,
) -> PlattFit:
    """Fit ``sigmoid(slope * logit(p) + intercept)`` by iteratively reweighted least squares.

    Two-parameter logistic regression is convex in ``(slope, intercept)``, so IRLS
    converges reliably from a sensible start with no tuning. Platt's target
    smoothing keeps it from saturating on the small labelled sets a decision panel
    actually accumulates.
    """
    if not confidences:
        return PlattFit(note="No labelled samples; identity mapping.")

    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must have the same length")

    targets = _smooth_targets([1.0 if item else 0.0 for item in correct])

    if len({1.0 if item else 0.0 for item in correct}) < 2:
        return PlattFit(note="All outcomes identical; a calibration map is not identifiable.")

    logits = [
        min(max(safe_logit(confidence), -MAX_FIT_LOGIT), MAX_FIT_LOGIT)
        for confidence in confidences
    ]

    # With no spread in the logits the slope is unidentifiable: every row has the
    # same score, so the likelihood is monotone in the slope and IRLS walks off to
    # a huge value (measured: slope 62 for a constant 0.95). The identifiable
    # answer is the base rate, and mapping every row to it is the *correct*
    # calibration when the confidence carries no information.
    if max(logits) - min(logits) < 1e-9:
        base_rate = _clamp_probability(sum(targets) / len(targets))
        return PlattFit(
            slope=0.0,
            intercept=safe_logit(base_rate),
            fitted=True,
            note=(
                "Every confidence in this sample was identical, so confidence carries no "
                f"information here. Rows are mapped to the observed base rate "
                f"({base_rate:.3f}) instead."
            ),
        )

    # A ridge on the slope keeps near-separable samples from producing an
    # arbitrarily steep map. The intercept is left unpenalised so the base rate is
    # not biased.
    penalty = ridge if ridge is not None else 0.01 + 1e-3 * len(logits)

    def penalised_objective(candidate_slope: float, candidate_intercept: float) -> float:
        return _platt_nll(
            candidate_slope, candidate_intercept, logits, targets
        ) + 0.5 * penalty * candidate_slope * candidate_slope

    # Start from the intercept-only fit: it already puts the mean in the right
    # place and keeps the first Newton step well conditioned.
    base_rate = _clamp_probability(sum(targets) / len(targets))
    slope, intercept = 1.0, safe_logit(base_rate)
    objective = penalised_objective(slope, intercept)

    for _ in range(iterations):
        s_xx = s_x1 = s_11 = t_x = t_1 = 0.0

        for x, target in zip(logits, targets, strict=True):
            eta = slope * x + intercept
            p = _clamp_probability(sigmoid(eta))
            weight = max(p * (1.0 - p), 1e-10)
            working = eta + (target - p) / weight
            s_xx += weight * x * x
            s_x1 += weight * x
            s_11 += weight
            t_x += weight * x * working
            t_1 += weight * working

        # Ridge on the slope only.
        s_xx += penalty

        determinant = s_xx * s_11 - s_x1 * s_x1
        if abs(determinant) < 1e-14:
            break

        step_slope = (t_x * s_11 - t_1 * s_x1) / determinant
        step_intercept = (s_xx * t_1 - s_x1 * t_x) / determinant

        if not (math.isfinite(step_slope) and math.isfinite(step_intercept)):
            break

        # Damped Newton: a full step can overshoot badly on a nearly separable
        # sample, so halve until the objective actually decreases. The objective is
        # convex, so this converges rather than oscillating.
        previous_slope, previous_intercept = slope, intercept

        scale = 1.0
        improved = False
        while scale > 1e-6:
            candidate_slope = slope + scale * (step_slope - slope)
            candidate_intercept = intercept + scale * (step_intercept - intercept)
            candidate = penalised_objective(candidate_slope, candidate_intercept)
            if candidate < objective:
                slope, intercept, objective = candidate_slope, candidate_intercept, candidate
                improved = True
                break
            scale *= 0.5

        if not improved:
            break

        # Convergence is measured against where the parameters *were*, not against
        # the step that was proposed. Comparing to the step is trivially true
        # whenever a full step is accepted, and would stop after one iteration.
        if (
            abs(slope - previous_slope) < tolerance
            and abs(intercept - previous_intercept) < tolerance
        ):
            break

    if not (math.isfinite(slope) and math.isfinite(intercept)):
        return PlattFit(note="The fit did not converge; identity mapping.")

    # A slope at or below zero means the reported confidence runs *against*
    # correctness. That is a finding about the labels or the question, not
    # something to paper over with a transform — so say so instead.
    if slope <= 0 or _correlation(logits, targets) <= 0:
        return PlattFit(
            note=(
                "On this sample the reported confidence is not positively related to "
                "correctness, so no monotone recalibration is applied. Investigate the "
                "labelling or the question definition before trusting any threshold."
            )
        )

    # A slope below this is a step function in disguise: the calibrated output
    # saturates to 0 or 1 for every row, which destroys a probability breakdown.
    # Fall back to the base rate rather than pretending to a sharpness the sample
    # cannot support.
    if slope < MIN_PLATT_SLOPE:
        base_rate = _clamp_probability(sum(targets) / len(targets))
        return PlattFit(
            slope=0.0,
            intercept=safe_logit(base_rate),
            fitted=True,
            note=(
                "This sample does not support a slope above that of a step function, so "
                f"rows are mapped to the observed base rate ({base_rate:.3f}) instead."
            ),
        )

    fit = PlattFit(
        slope=min(slope, MAX_PLATT_SLOPE),
        intercept=min(max(intercept, -MAX_PLATT_INTERCEPT), MAX_PLATT_INTERCEPT),
        fitted=True,
    )

    # IRLS can land somewhere unhelpful on separable or tiny samples. Rather than
    # trust the parameters, check that the map actually scores better than doing
    # nothing — if it does not, doing nothing is the honest answer.
    identity_objective = _platt_nll(1.0, 0.0, logits, targets)
    fitted_objective = _platt_nll(fit.slope, fit.intercept, logits, targets)
    if fitted_objective >= identity_objective:
        return PlattFit(
            note=(
                "The fitted map did not improve on the raw confidence for this sample, so "
                "no recalibration is applied."
            )
        )

    fit.note = fit.describe()
    return fit


def apply_map_to_distribution(
    distribution: dict[str, float], mapper
) -> dict[str, float]:
    """Apply a monotone scalar map to every probability, then renormalise.

    Because :class:`PlattFit` is constrained to a positive slope it is
    rank-preserving, so this cannot reorder the options within a distribution —
    which is the property that makes it safe to apply to a breakdown as well as to
    a single headline number.
    """
    if not distribution:
        return {}

    mapped = {label: max(_clamp_probability(mapper(value)), EPS) for label, value in distribution.items()}
    total = sum(mapped.values())
    if total <= 0:  # pragma: no cover - clamping keeps this positive
        return dict(distribution)
    return {label: value / total for label, value in mapped.items()}


def apply_platt_to_distribution(
    distribution: dict[str, float], fit: PlattFit
) -> dict[str, float]:
    if fit.is_identity:
        return {label: float(value) for label, value in distribution.items()}
    return apply_map_to_distribution(distribution, fit.apply)


def expected_calibration_error(
    confidences: list[float], correct: list[bool | int], n_bins: int = 10
) -> float | None:
    report = calibration_report(confidences, correct, n_bins=n_bins)
    return report.ece
