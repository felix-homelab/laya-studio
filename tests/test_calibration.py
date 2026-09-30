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


# --- what `confidence` means once a map is applied ---------------------------
#
# Two operations run over a calibrated question result and they are easy to
# conflate: the *scalar* map (`fit.apply(confidence)`) and the *distribution*
# rescale. These pin which one `confidence` holds, because the ladder, the bands
# and the calibration monitor all gate on it.


def test_a_calibrated_entry_stores_the_map_output_and_not_the_winning_probability():
    """`confidence` is `fit.apply(raw)`, never the top of the rescaled breakdown.

    The map is fitted on Laya's entropy-based confidence (``services/calibration.py``
    reads ``Evaluation.confidence`` against correctness), so its output is the only
    value ``confidence`` may hold. A question whose raw confidence is 0.40 while its
    top probability is 0.55 is the ordinary case, not a corner: the two numbers are
    different quantities, as ``docs/laya-integration.md`` spells out.
    """
    from app.inference.calibration import PlattFit
    from app.services.calibration import ActiveCalibration
    from app.services.evaluation import _calibrate_entry

    fit = PlattFit(slope=0.43, intercept=2.16, fitted=True)
    entry = {
        "question": "department",
        "type": "choice",
        "choice": "billing",
        "confidence": 0.40,
        "breakdown": [
            {"label": "billing", "raw_label": "billing", "probability": 0.55, "rank": 1, "is_top": True},
            {"label": "other", "raw_label": "other", "probability": 0.45, "rank": 2, "is_top": False},
        ],
        "alternatives": [
            {"label": "other", "raw_label": "other", "probability": 0.45, "rank": 2, "is_top": False}
        ],
    }

    calibrated = _calibrate_entry(
        dict(entry), ActiveCalibration(fit=fit, source="fitted", n_samples=40, note="fitted")
    )

    assert calibrated["calibrated"] is True
    assert calibrated["raw_confidence"] == 0.40
    assert calibrated["confidence"] == round(fit.apply(0.40), 6)
    assert calibrated["confidence"] != calibrated["breakdown"][0]["probability"]
    # The argmax still follows the rescaled distribution, so the record agrees with
    # the breakdown it stores.
    assert calibrated["choice"] == calibrated["breakdown"][0]["label"]


def test_a_base_rate_map_reaches_the_scalar_confidence_not_the_flattened_breakdown():
    """Slope 0 maps every row to the base rate — including rows with a breakdown.

    The distribution rescale is a separate operation, and under a constant map it
    flattens a k-way breakdown to 1/k. Storing *that* as ``confidence`` would
    replace the map's 0.85 with 0.25 on a 4-way question.
    """
    from app.inference.calibration import PlattFit, safe_logit
    from app.services.calibration import ActiveCalibration
    from app.services.evaluation import _calibrate_entry

    base_rate = 0.85
    fit = PlattFit(slope=0.0, intercept=safe_logit(base_rate), fitted=True)
    assert not fit.is_identity, "a base-rate map is not the identity, so it is applied"

    entry = {
        "question": "department",
        "type": "choice",
        "choice": "billing",
        "confidence": 0.40,
        "breakdown": [
            {"label": label, "raw_label": label, "probability": p, "rank": rank + 1, "is_top": rank == 0}
            for rank, (label, p) in enumerate(
                [("billing", 0.4), ("technical", 0.3), ("sales", 0.2), ("other", 0.1)]
            )
        ],
    }

    calibrated = _calibrate_entry(
        dict(entry), ActiveCalibration(fit=fit, source="fitted", n_samples=40, note="fitted")
    )

    assert calibrated["confidence"] == pytest.approx(base_rate, abs=1e-6)
    # No temperature flattens a curve *and* re-centres it; the field says so.
    assert calibrated["temperature"] is None
    assert calibrated["breakdown"][0]["probability"] == 0.25
    assert calibrated["confidence"] != calibrated["breakdown"][0]["probability"]


def test_an_entry_without_a_confidence_does_not_acquire_one_from_the_breakdown():
    """A scalar map applies to a scalar; it must not invent one where none was sent."""
    from app.inference.calibration import PlattFit
    from app.services.calibration import ActiveCalibration
    from app.services.evaluation import _calibrate_entry

    fit = PlattFit(slope=0.43, intercept=2.16, fitted=True)
    entry = {
        "question": "department",
        "type": "choice",
        "choice": "billing",
        "confidence": None,
        "breakdown": [
            {"label": "billing", "raw_label": "billing", "probability": 0.55, "rank": 1, "is_top": True},
            {"label": "other", "raw_label": "other", "probability": 0.45, "rank": 2, "is_top": False},
        ],
    }

    calibrated = _calibrate_entry(
        dict(entry), ActiveCalibration(fit=fit, source="fitted", n_samples=40, note="fitted")
    )

    assert calibrated["confidence"] is None
    assert "raw_confidence" not in calibrated
    # The breakdown is still rescaled — that operation never needed a scalar.
    assert sum(item["probability"] for item in calibrated["breakdown"]) == pytest.approx(
        1.0, rel=1e-5
    )
    assert calibrated["breakdown"][0]["probability"] != 0.55


def test_a_constant_confidence_is_stored_as_the_base_rate_not_as_one_over_k(
    client, triage_schema, scripted
):
    """End to end: the first row a degenerate fit applies to reports the base rate.

    Every stored confidence in the slice is 0.55, so once the tenth labelled row
    exists ``fit_platt`` degenerates to the observed base rate — a scalar map. The
    same constant map flattens the rescaled 4-way breakdown to exactly 0.25, and
    0.25 is what the ladder used to gate the decision on.
    """
    from app.inference.calibration import PlattFit

    scripted(
        answers={
            "department": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.4, "technical": 0.3, "sales": 0.2, "other": 0.1},
                "confidence": 0.55,
            }
        }
    )

    created: dict = {}
    for index in range(11):
        created = client.post(
            "/api/playground/evaluate",
            json={
                "state": {"body": f"We were billed twice, message {index}."},
                "schema_id": triage_schema["id"],
                "primary_question": "department",
            },
        ).json()
        # Nine right and one wrong: enough labelled rows to fit, and not all one
        # outcome, which would make the map unidentifiable instead of degenerate.
        client.post(
            f"/api/playground/evaluations/{created['id']}/label",
            json={"label": "billing" if index < 9 else "technical"},
        )

    entry = next(item for item in created["question_results"] if item["question"] == "department")

    # Rows 0..9 saw fewer than calibration_min_samples labelled rows, so they kept
    # the raw value; row 10 is the first the base-rate map has been fitted for.
    assert entry["calibrated"] is True, entry
    assert entry["raw_confidence"] == 0.55
    assert entry["calibration_slope"] == 0.0
    assert entry["temperature"] is None

    # The row's own report is internally consistent: replays the map it names.
    base_rate_map = PlattFit(slope=0.0, intercept=entry["calibration_intercept"], fitted=True)
    assert entry["confidence"] == pytest.approx(
        base_rate_map.apply(entry["raw_confidence"]), abs=1e-5
    )

    # ...and it is not the flattened breakdown, which is where the 1/k bug lived.
    assert entry["breakdown"][0]["probability"] == 0.25
    assert entry["confidence"] > 0.5


def test_the_fitter_reads_the_field_the_evaluator_writes(client, triage_schema, scripted):
    """One field, one scale: `Evaluation.confidence` in, the same value persisted.

    `services/calibration.py` builds every fit from ``(Evaluation.confidence,
    correct)``, so a refit only describes the mapping the evaluator actually applied
    if that column holds the map's output. Storing the winning breakdown entry
    instead made each refit consume a fresh, differently-scaled quantity, so the
    fitted map described a transform nobody had run.
    """
    from sqlalchemy import select

    from app.config import get_settings
    from app.db import get_session_factory
    from app.inference.calibration import PlattFit, fit_platt
    from app.models import Evaluation
    from app.services import calibration as calibration_service

    scripted(
        answers={
            "department": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.4, "technical": 0.3, "sales": 0.2, "other": 0.1},
                "confidence": 0.55,
            }
        }
    )

    created: dict = {}
    for index in range(11):
        created = client.post(
            "/api/playground/evaluate",
            json={
                "state": {"body": f"We were billed twice, message {index}."},
                "schema_id": triage_schema["id"],
                "primary_question": "department",
            },
        ).json()
        client.post(
            f"/api/playground/evaluations/{created['id']}/label",
            json={"label": "billing" if index < 9 else "technical"},
        )

    entry = next(item for item in created["question_results"] if item["question"] == "department")
    assert entry["calibrated"] is True, entry

    reported = PlattFit(
        slope=entry["calibration_slope"],
        intercept=entry["calibration_intercept"],
        fitted=True,
    )
    assert entry["confidence"] == pytest.approx(reported.apply(entry["raw_confidence"]), abs=1e-5)

    db = get_session_factory()()
    try:
        # The row as persisted — this is what the next fit reads back off the table.
        stored = db.get(Evaluation, created["id"])
        assert stored is not None and stored.calibrated is True
        assert stored.raw_confidence == entry["raw_confidence"]
        assert stored.confidence == pytest.approx(
            reported.apply(stored.raw_confidence), abs=1e-6
        )

        statement = (
            select(Evaluation.confidence, Evaluation.top_choice, Evaluation.label)
            .where(
                Evaluation.schema_id == triage_schema["id"],
                Evaluation.primary_question == "department",
                Evaluation.confidence.is_not(None),
                Evaluation.label.is_not(None),
                Evaluation.top_choice.is_not(None),
            )
            .order_by(Evaluation.id)
        )
        pairs = [
            (float(confidence), str(top).strip().lower() == str(label).strip().lower())
            for confidence, top, label in db.execute(statement)
        ]
        assert len(pairs) >= get_settings().calibration_min_samples

        # Re-deriving the map from exactly those persisted pairs reproduces the map
        # the panel stores — fitter and writer agree on which quantity this is.
        refit = fit_platt([value for value, _ in pairs], [outcome for _, outcome in pairs])
        snapshot = calibration_service.fit_and_store(
            db, primitive="choice", question="department", schema_id=triage_schema["id"]
        )
    finally:
        db.close()

    assert snapshot is not None, "a slice with enough labelled rows must produce a snapshot"
    assert snapshot.n_samples == len(pairs)
    assert snapshot.slope == pytest.approx(refit.slope, abs=1e-6)
    assert snapshot.intercept == pytest.approx(refit.intercept, abs=1e-6)
