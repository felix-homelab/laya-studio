"""DB-backed calibration: fit temperatures from labelled evaluations and cache them.

The pure maths lives in :mod:`app.inference.calibration`. This module answers the
operational questions: *do we have enough labelled evidence to trust a refit?*
*which temperature should the evaluator apply right now?* *what should the
calibration monitor display?*

Fitting is per (schema, question) with a schema-wide fallback, because a single
temperature per checkpoint is exactly the coarse treatment Laya's own model card
recommends against — the over-confidence differs by question type and option
count.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..inference.calibration import (
    CalibrationReport,
    PlattFit,
    calibration_report,
    fit_platt,
)
from ..models import CalibrationSnapshot, EvalSchema, Evaluation

logger = logging.getLogger(__name__)

#: Reliability-diagram resolution.
N_BINS = 10

@dataclass(slots=True)
class ActiveCalibration:
    """The recalibration map the evaluator should apply, and why."""

    fit: PlattFit = field(default_factory=PlattFit)
    #: "identity" | "fitted" | "cached"
    source: str = "identity"
    n_samples: int = 0
    note: str = ""

    @property
    def is_identity(self) -> bool:
        return self.fit.is_identity

    @property
    def temperature(self) -> float | None:
        """``1 / slope`` — a familiar single number, for display only."""
        return self.fit.temperature

    def apply(self, confidence: float | None) -> float | None:
        if confidence is None:
            return None
        return self.fit.apply(confidence)

    def to_dict(self) -> dict:
        return {
            "slope": round(self.fit.slope, 6),
            "intercept": round(self.fit.intercept, 6),
            "temperature": (
                None if self.temperature is None else round(self.temperature, 6)
            ),
            "source": self.source,
            "n_samples": self.n_samples,
            "note": self.note,
        }


_lock = threading.Lock()
#: The resolved calibration (fit plus its sample count and note) and its expiry.
_cache: dict[tuple[int | None, str], tuple[ActiveCalibration, datetime]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _labelled_pairs(
    db: Session,
    *,
    schema_id: int | None,
    question: str | None,
) -> tuple[list[float], list[bool]]:
    """Collect (confidence, was_correct) pairs for a fit or a report."""
    statement = select(Evaluation.confidence, Evaluation.top_choice, Evaluation.label).where(
        Evaluation.confidence.is_not(None),
        Evaluation.label.is_not(None),
        Evaluation.top_choice.is_not(None),
    )
    if schema_id is not None:
        statement = statement.where(Evaluation.schema_id == schema_id)
    if question:
        statement = statement.where(Evaluation.primary_question == question)

    confidences: list[float] = []
    correct: list[bool] = []
    for confidence, top_choice, label in db.execute(statement):
        if confidence is None or top_choice is None or label is None:
            continue
        confidences.append(float(confidence))
        correct.append(str(top_choice).strip().lower() == str(label).strip().lower())
    return confidences, correct


def report_for(
    db: Session,
    *,
    schema_id: int | None = None,
    question: str | None = None,
) -> CalibrationReport:
    """Reliability report for a schema/question slice."""
    confidences, correct = _labelled_pairs(db, schema_id=schema_id, question=question)
    return calibration_report(confidences, correct, n_bins=N_BINS)


def fit_and_store(
    db: Session,
    *,
    primitive: str,
    question: str = "",
    schema_id: int | None = None,
    commit: bool = True,
) -> CalibrationSnapshot | None:
    """Fit a temperature for one slice and persist a snapshot.

    Returns ``None`` when there is not enough labelled evidence to bother.
    """
    settings = get_settings()
    confidences, correct = _labelled_pairs(db, schema_id=schema_id, question=question or None)

    if len(confidences) < settings.calibration_min_samples:
        return None

    report = calibration_report(confidences, correct, n_bins=N_BINS)
    fit = fit_platt(confidences, correct)

    snapshot = CalibrationSnapshot(
        primitive=primitive,
        question=question or "",
        schema_id=schema_id,
        n_samples=report.n_samples,
        slope=fit.slope,
        intercept=fit.intercept,
        temperature=fit.temperature,
        ece=report.ece,
        mce=report.mce,
        brier=report.brier,
        accuracy=report.accuracy,
        mean_confidence=report.mean_confidence,
        bins=[item.to_dict() for item in report.bins],
    )
    db.add(snapshot)

    if commit:
        db.commit()
        db.refresh(snapshot)

    logger.info(
        "Calibration fitted for schema=%s question=%r: slope=%.4f intercept=%+.4f "
        "(ECE %.4f) over %d samples",
        schema_id,
        question,
        fit.slope,
        fit.intercept,
        report.ece or 0.0,
        report.n_samples,
    )
    return snapshot


def get_active_calibration(
    db: Session,
    *,
    schema_id: int | None,
    question: str | None,
) -> ActiveCalibration:
    """Resolve the temperature to apply now, using the cache then a fresh fit.

    Keyed by (schema, question): a question name has one type within a schema,
    so the primitive adds nothing to the key.
    """
    settings = get_settings()
    if not settings.calibration_enabled:
        return ActiveCalibration(note="Calibration disabled by configuration.")

    cache_key = (schema_id, question or "")
    now = datetime.now(timezone.utc)

    with _lock:
        cached = _cache.get(cache_key)
    if cached is not None and cached[1] > now:
        stored, _ = cached
        # Keep the sample count and note from the fit that was cached.
        return ActiveCalibration(
            fit=stored.fit,
            source="cached",
            n_samples=stored.n_samples,
            note=stored.note or ("Cached identity mapping." if stored.is_identity else ""),
        )

    confidences, correct = _labelled_pairs(db, schema_id=schema_id, question=question)

    # Fall back to the schema-wide fit when the specific question is thin.
    if len(confidences) < settings.calibration_min_samples and question:
        confidences, correct = _labelled_pairs(db, schema_id=schema_id, question=None)
        note_prefix = "Fell back to the schema-wide fit: "
    else:
        note_prefix = ""

    if len(confidences) < settings.calibration_min_samples:
        result = ActiveCalibration(
            source="identity",
            n_samples=len(confidences),
            note=(
                f"{len(confidences)} labelled samples; "
                f"{settings.calibration_min_samples} required before refitting."
            ),
        )
    else:
        fit = fit_platt(confidences, correct)
        result = ActiveCalibration(
            fit=fit,
            source="fitted",
            n_samples=len(confidences),
            note=note_prefix + fit.note,
        )

    expiry = now + timedelta(seconds=max(settings.calibration_cache_ttl, 0))
    with _lock:
        # Expired entries are only skipped on read; drop them here so a stream of
        # distinct (schema, question) keys cannot grow the cache without bound.
        for key in [key for key, (_, expires) in _cache.items() if expires <= now]:
            del _cache[key]
        _cache[cache_key] = (result, expiry)
    return result


def latest_snapshots(db: Session, *, schema_id: int | None = None, limit: int = 50):
    """Most recent calibration snapshot per (schema, question) slice."""
    # Rank within each slice in SQL, so `limit` counts distinct slices however
    # many snapshots the busiest slices have accumulated.
    rank = (
        func.row_number()
        .over(
            partition_by=(CalibrationSnapshot.schema_id, CalibrationSnapshot.question),
            order_by=(CalibrationSnapshot.created_at.desc(), CalibrationSnapshot.id.desc()),
        )
        .label("rank")
    )
    ranked = select(CalibrationSnapshot.id, rank)
    if schema_id is not None:
        ranked = ranked.where(CalibrationSnapshot.schema_id == schema_id)
    latest = ranked.subquery()

    statement = (
        select(CalibrationSnapshot)
        .join(latest, latest.c.id == CalibrationSnapshot.id)
        .where(latest.c.rank == 1)
        .order_by(CalibrationSnapshot.created_at.desc(), CalibrationSnapshot.id.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


def snapshot_count(db: Session) -> int:
    return int(db.scalar(select(func.count(CalibrationSnapshot.id))) or 0)


def refresh_all(db: Session, *, commit: bool = True) -> list[CalibrationSnapshot]:
    """Refit every (schema, primary question) slice that has labelled rows."""
    statement = (
        select(Evaluation.schema_id, Evaluation.primary_question)
        .where(
            Evaluation.label.is_not(None),
            Evaluation.confidence.is_not(None),
            Evaluation.primary_question.is_not(None),
        )
        .group_by(Evaluation.schema_id, Evaluation.primary_question)
    )

    snapshots: list[CalibrationSnapshot] = []
    for schema_id, question in db.execute(statement):
        if not question:
            continue
        schema = db.get(EvalSchema, schema_id) if schema_id else None
        primitive = schema.primitive if schema is not None else "choice"
        snapshot = fit_and_store(
            db,
            primitive=primitive,
            question=str(question),
            schema_id=schema_id,
            commit=False,
        )
        if snapshot is not None:
            snapshots.append(snapshot)

    if commit and snapshots:
        db.commit()
        for snapshot in snapshots:
            db.refresh(snapshot)

    clear_cache()
    return snapshots
