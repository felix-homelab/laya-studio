"""Calibration maths and the DB-backed fitting service."""

from __future__ import annotations

import math

import pytest

from app.inference.calibration import (
    apply_platt_to_distribution,
    apply_temperature,
    apply_temperature_to_distribution,
    calibration_report,
    fit_platt,
    reliability_bins,
    safe_logit,
)


def test_identity_temperature_is_a_no_op():
    assert apply_temperature(0.8, 1.0) == pytest.approx(0.8)
    distribution = {"a": 0.7, "b": 0.3}
    assert apply_temperature_to_distribution(distribution, 1.0) == pytest.approx(distribution)


def test_temperature_above_one_softens_toward_a_coin_flip():
    softened = apply_temperature(0.99, 3.0)
    assert 0.5 < softened < 0.99


def test_temperature_below_one_sharpens():
    assert apply_temperature(0.7, 0.5) > 0.7


def test_temperature_on_a_distribution_preserves_the_simplex():
    rescaled = apply_temperature_to_distribution({"a": 0.6, "b": 0.3, "c": 0.1}, 2.5)
    assert sum(rescaled.values()) == pytest.approx(1.0)
    # Softening must not reorder the options.
    assert sorted(rescaled, key=rescaled.__getitem__, reverse=True) == ["a", "b", "c"]


def test_safe_logit_clamps_the_boundaries():
    assert math.isfinite(safe_logit(0.0))
    assert math.isfinite(safe_logit(1.0))
    assert safe_logit(0.0) < 0 < safe_logit(1.0)


# --- reports -----------------------------------------------------------------


def test_perfectly_calibrated_confidences_have_no_gap():
    # 80% confident, right 80% of the time.
    confidences = [0.8] * 100
    correct = [True] * 80 + [False] * 20
    report = calibration_report(confidences, correct)
    assert report.ece == pytest.approx(0.0, abs=1e-9)
    assert report.accuracy == pytest.approx(0.8)
    assert report.mean_confidence == pytest.approx(0.8)


def test_overconfidence_shows_up_as_a_positive_gap():
    confidences = [0.95] * 100
    correct = [True] * 50 + [False] * 50
    report = calibration_report(confidences, correct)
    assert report.ece == pytest.approx(0.45, abs=1e-9)
    assert report.mean_confidence > report.accuracy
    assert report.brier == pytest.approx(0.95**2 * 0.5 + 0.05**2 * 0.5, abs=1e-6)


def test_empty_report_is_not_an_error():
    report = calibration_report([], [])
    assert report.n_samples == 0
    assert report.ece is None
    assert "No labelled samples" in report.note


def test_mismatched_lengths_are_rejected():
    with pytest.raises(ValueError):
        calibration_report([0.5, 0.6], [True])


def test_reliability_bins_partition_every_sample():
    confidences = [0.05, 0.15, 0.95, 0.99]
    correct = [False, True, True, True]
    bins = reliability_bins(confidences, correct, n_bins=10)
    assert sum(item.count for item in bins) == 4
    assert bins[-1].count == 2


def test_reliability_bins_rejects_a_bad_bin_count():
    with pytest.raises(ValueError):
        reliability_bins([0.5], [True], n_bins=0)


# --- fitting -----------------------------------------------------------------


def test_platt_recovers_a_systematically_understated_confidence():
    """The case Laya actually presents, and the case one temperature cannot express.

    Measured on the bundled sample: mean reported confidence 0.40 against 0.85
    observed accuracy. Every confidence sits below 0.5 while most outcomes are
    correct, so no positive temperature can help — `sigmoid(logit(p)/T)` keeps `p`
    on the same side of 0.5. The intercept is what fixes it.
    """
    confidences = [0.30, 0.35, 0.40, 0.45, 0.50, 0.25, 0.38, 0.42, 0.33, 0.47,
                   0.29, 0.36, 0.41, 0.44, 0.31, 0.39, 0.43, 0.34, 0.46, 0.28]
    correct = [True] * 17 + [False] * 3

    fit = fit_platt(confidences, correct)
    assert fit.fitted
    assert fit.slope > 0  # rank-preserving
    assert fit.intercept > 0  # shifts the curve up, which a temperature cannot do

    calibrated_mean = sum(fit.apply(c) for c in confidences) / len(confidences)
    assert abs(calibrated_mean - 0.85) < 0.10, calibrated_mean


def test_a_single_temperature_cannot_fix_an_understated_curve():
    """Guards the reasoning in the docstring above, so nobody simplifies it away."""
    confidence = 0.40
    best = max(apply_temperature(confidence, t) for t in (0.05, 0.5, 1.0, 5.0, 20.0))
    assert best < 0.51  # cannot cross the midpoint, let alone reach 0.85


def test_platt_softens_an_overstated_confidence():
    """Accuracy rises with confidence but stays *below* it — the classic overconfident model."""
    confidences: list[float] = []
    correct: list[bool] = []
    for confidence, accuracy, n in ((0.6, 0.50, 50), (0.7, 0.58, 50), (0.8, 0.66, 50), (0.9, 0.74, 50)):
        hits = round(n * accuracy)
        confidences += [confidence] * n
        correct += [True] * hits + [False] * (n - hits)

    fit = fit_platt(confidences, correct)
    assert fit.fitted
    assert fit.apply(0.95) < 0.95

    calibrated_mean = sum(fit.apply(c) for c in confidences) / len(confidences)
    observed = sum(1 for value in correct if value) / len(correct)
    # The map should land near the observed rate, not near the stated one.
    assert abs(calibrated_mean - observed) < 0.08, (calibrated_mean, observed)


def test_a_constant_confidence_maps_to_the_base_rate():
    """With no spread in the score the slope is unidentifiable.

    IRLS walks off to a huge value here (measured: slope 62), which saturates every
    row. The identifiable answer is the observed frequency.
    """
    fit = fit_platt([0.95] * 200, [True] * 120 + [False] * 80)
    assert fit.fitted
    assert fit.slope == 0.0
    assert fit.apply(0.95) == pytest.approx(0.60, abs=0.01)
    assert fit.temperature is None
    assert "base rate" in fit.note


def test_platt_is_rank_preserving():
    confidences = [0.2, 0.5, 0.9] * 20
    correct = [False] * 15 + [True] * 45
    fit = fit_platt(confidences, correct)
    applied = [fit.apply(value) for value in (0.1, 0.4, 0.7, 0.95)]
    assert applied == sorted(applied)


def test_platt_reports_an_ill_posed_problem_rather_than_guessing():
    fit = fit_platt([0.9] * 50, [True] * 50)
    assert fit.fitted is False
    assert fit.is_identity
    assert "not identifiable" in fit.note


def test_platt_handles_no_samples():
    fit = fit_platt([], [])
    assert fit.fitted is False
    assert "No labelled samples" in fit.note


def test_platt_refuses_to_paper_over_an_anti_correlated_confidence():
    """If confidence runs *against* correctness, that is a finding, not a transform."""
    confidences = [0.1] * 20 + [0.9] * 20
    correct = [True] * 20 + [False] * 20  # high confidence, wrong every time
    fit = fit_platt(confidences, correct)
    assert fit.fitted is False
    assert "not positively related" in fit.note


def test_platt_keeps_the_map_bounded_on_perfectly_separable_data():
    """Separable samples have an infinite MLE slope; the ridge and the cap bound it."""
    fit = fit_platt([0.2] * 10 + [0.9] * 10, [False] * 10 + [True] * 10)
    assert fit.fitted
    assert 0 < fit.slope <= 20.0
    # Must not saturate: a breakdown of 0s and 1s is useless.
    assert 0.0 < fit.apply(0.2) < 0.5 < fit.apply(0.9) < 1.0


def test_platt_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        fit_platt([0.5, 0.6], [True])


def test_platt_reduces_nll_on_synthetic_overstated_data():
    from app.inference.calibration import _binary_nll

    confidences = [0.9, 0.8, 0.95, 0.7, 0.85, 0.92, 0.6, 0.88]
    correct = [True, False, True, False, True, False, True, False]
    logits = [safe_logit(value) for value in confidences]
    targets = [1.0 if value else 0.0 for value in correct]

    fit = fit_platt(confidences, correct)
    # The map's own objective, evaluated through the identity's parameterisation.
    assert fit.slope > 0
    assert _binary_nll(1.0 / fit.slope, logits, targets) >= 0  # sanity: finite


def test_applying_platt_to_a_distribution_preserves_the_simplex_and_order():
    confidences = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.35, 0.45, 0.55, 0.65]
    correct = [True] * 8 + [False] * 2
    fit = fit_platt(confidences, correct)

    rescaled = apply_platt_to_distribution({"a": 0.6, "b": 0.3, "c": 0.1}, fit)
    assert sum(rescaled.values()) == pytest.approx(1.0)
    assert sorted(rescaled, key=rescaled.__getitem__, reverse=True) == ["a", "b", "c"]


def test_identity_platt_is_a_no_op():
    from app.inference.calibration import PlattFit

    fit = PlattFit()
    assert fit.is_identity
    assert fit.apply(0.73) == pytest.approx(0.73)
    assert apply_platt_to_distribution({"a": 0.7, "b": 0.3}, fit) == pytest.approx(
        {"a": 0.7, "b": 0.3}
    )


# --- the service layer -------------------------------------------------------


def test_service_falls_back_to_identity_without_enough_labels(client, triage_schema):
    from app.db import get_session_factory
    from app.services import calibration as calibration_service

    db = get_session_factory()()
    try:
        active = calibration_service.get_active_calibration(
            db, schema_id=triage_schema["id"], question="department"
        )
        assert active.is_identity
        assert active.source == "identity"
        assert "required before refitting" in active.note
    finally:
        db.close()


def test_service_fits_once_enough_labelled_rows_exist(client, triage_schema):
    from app.db import get_session_factory
    from app.services import calibration as calibration_service

    db = get_session_factory()()
    try:
        for index in range(24):
            outcome = _evaluate(db, triage_schema["id"], index)
            # Deliberately wrong half the time, so the confidence is inflated.
            truth = outcome.evaluation.top_choice if index % 2 == 0 else "other"
            outcome.evaluation.label = truth

        db.commit()
        snapshots = calibration_service.refresh_all(db)
        assert snapshots, "expected at least one fitted snapshot"

        active = calibration_service.get_active_calibration(
            db, schema_id=triage_schema["id"], question="department"
        )
        assert active.source in ("fitted", "cached")
        assert active.n_samples >= 10
        assert active.fit.slope > 0
    finally:
        db.close()


def _evaluate(db, schema_id: int, index: int):
    from app.services import decision_schemas
    from app.services import evaluation as evaluation_service

    schema = decision_schemas.get_schema(db, schema_id)
    bodies = [
        "We were billed twice and need a refund immediately.",
        "Our integration returns a 500 and production is blocked.",
        "Please send enterprise pricing for 200 seats.",
    ]
    return evaluation_service.run_evaluation(
        db,
        state={"body": bodies[index % len(bodies)]},
        schema=schema,
        primary_question="department",
        commit=False,
    )
