"""Analytics for the confidence dashboard and calibration monitor.

Aggregation is done in Python rather than SQL on purpose. The bucketing, the
JSONB routing extraction and the confidence-band counts would each need a
SQLite/PostgreSQL dialect branch in SQL; doing it once in Python keeps the two
backends behaving identically and keeps the metrics unit-testable without a
database. Row volume is bounded by ``MAX_ANALYTICS_ROWS`` and documented in
``docs/ROADMAP.md`` as the first thing to move server-side.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..inference.calibration import calibration_report
from ..models import Evaluation, ReviewItem, WorkflowRun

logger = logging.getLogger(__name__)

#: Guard rail: analytics is an operator view, not a reporting warehouse.
MAX_ANALYTICS_ROWS = 50_000

DEFAULT_WINDOW_HOURS = 24 * 7


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; everything in this app is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


@dataclass(slots=True)
class AnalyticsWindow:
    hours: int = DEFAULT_WINDOW_HOURS
    start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def build(cls, hours: int = DEFAULT_WINDOW_HOURS) -> AnalyticsWindow:
        end = datetime.now(timezone.utc)
        return cls(hours=hours, start=end - timedelta(hours=max(hours, 1)), end=end)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


def _evaluations(db: Session, window: AnalyticsWindow) -> list[Evaluation]:
    statement = (
        select(Evaluation)
        .where(Evaluation.created_at >= window.start)
        .order_by(Evaluation.created_at.asc())
        .limit(MAX_ANALYTICS_ROWS)
    )
    return list(db.scalars(statement))


def confidence_histogram(
    confidences: list[float],
    *,
    n_bins: int = 10,
    auto_threshold: float | None = None,
    verify_threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Equal-width histogram, flagged with which side of the ladder it falls on."""
    settings = get_settings()
    auto_threshold = settings.auto_threshold if auto_threshold is None else auto_threshold
    verify_threshold = settings.verify_threshold if verify_threshold is None else verify_threshold

    width = 1.0 / n_bins
    buckets = [0] * n_bins
    for confidence in confidences:
        index = min(int(max(0.0, min(1.0, confidence)) / width), n_bins - 1)
        buckets[index] += 1

    total = sum(buckets) or 1
    return [
        {
            "lower": round(index * width, 4),
            "upper": round((index + 1) * width, 4),
            "count": count,
            "share": round(count / total, 6),
            "band": (
                "auto"
                if index * width >= auto_threshold
                else "verify"
                if index * width >= verify_threshold
                else "review"
            ),
        }
        for index, count in enumerate(buckets)
    ]


def band_counts(confidences: Iterable[float]) -> dict[str, int]:
    settings = get_settings()
    counts = {"auto": 0, "verify": 0, "review": 0}
    for confidence in confidences:
        if confidence >= settings.auto_threshold:
            counts["auto"] += 1
        elif confidence >= settings.verify_threshold:
            counts["verify"] += 1
        else:
            counts["review"] += 1
    return counts


def overview(db: Session, *, hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """Headline metrics for the dashboard header cards."""
    window = AnalyticsWindow.build(hours)
    evaluations = _evaluations(db, window)

    if not evaluations:
        return {
            "window": window.to_dict(),
            "total": 0,
            "note": "No evaluations in this window yet. Run something in the playground.",
        }

    confidences = [item.confidence for item in evaluations if item.confidence is not None]
    latencies = [item.latency_ms for item in evaluations if item.latency_ms is not None]
    labelled = [item for item in evaluations if item.label and item.top_choice]
    correct = [
        str(item.top_choice).strip().lower() == str(item.label).strip().lower() for item in labelled
    ]
    labelled_confidences = [item.confidence for item in labelled if item.confidence is not None]

    report = (
        calibration_report(labelled_confidences, correct)
        if labelled and len(labelled_confidences) == len(labelled)
        else None
    )

    by_source = Counter(item.source for item in evaluations)
    by_model = Counter(
        str((item.routing or {}).get("model") or item.engine_model) for item in evaluations
    )
    bands = band_counts(confidences)
    routed_total = sum(bands.values()) or 1

    return {
        "window": window.to_dict(),
        "total": len(evaluations),
        "calibrated_share": round(
            sum(1 for item in evaluations if item.calibrated) / len(evaluations), 6
        ),
        "by_source": dict(by_source),
        "by_model": dict(by_model),
        "confidence": {
            "mean": round(mean(confidences), 6) if confidences else None,
            "median": round(median(confidences), 6) if confidences else None,
            "p95": round(_percentile(confidences, 0.95), 6) if confidences else None,
        },
        "latency_ms": {
            "mean": round(mean(latencies), 3) if latencies else None,
            "median": round(median(latencies), 3) if latencies else None,
            "p95": round(_percentile(latencies, 0.95), 3) if latencies else None,
        },
        "bands": bands,
        "escalation_rate": round(bands["review"] / routed_total, 6),
        "verify_rate": round(bands["verify"] / routed_total, 6),
        "auto_rate": round(bands["auto"] / routed_total, 6),
        "labelled": len(labelled),
        "accuracy": (
            round(sum(1 for item in correct if item) / len(correct), 6) if correct else None
        ),
        "ece": report.ece if report else None,
        "brier": report.brier if report else None,
        "calibration_note": report.note if report else "No labelled rows yet.",
        "top_choices": [
            {"label": label, "count": count}
            for label, count in Counter(
                item.top_choice for item in evaluations if item.top_choice
            ).most_common(10)
        ],
    }


def timeseries(
    db: Session,
    *,
    hours: int = DEFAULT_WINDOW_HOURS,
    bucket: str = "hour",
) -> list[dict[str, Any]]:
    """Volume, mean confidence and escalation rate over time."""
    window = AnalyticsWindow.build(hours)
    evaluations = _evaluations(db, window)
    if not evaluations:
        return []

    if bucket not in ("hour", "day"):
        raise ValueError("bucket must be 'hour' or 'day'")

    grouped: dict[datetime, list[Evaluation]] = {}
    for evaluation in evaluations:
        created = _as_utc(evaluation.created_at)
        if bucket == "hour":
            key = created.replace(minute=0, second=0, microsecond=0)
        else:
            key = created.replace(hour=0, minute=0, second=0, microsecond=0)
        grouped.setdefault(key, []).append(evaluation)

    series: list[dict[str, Any]] = []
    for key in sorted(grouped):
        items = grouped[key]
        confidences = [item.confidence for item in items if item.confidence is not None]
        bands = band_counts(confidences)
        total = sum(bands.values()) or 1
        series.append(
            {
                "bucket": key.isoformat(),
                "count": len(items),
                "mean_confidence": round(mean(confidences), 6) if confidences else None,
                "auto": bands["auto"],
                "verify": bands["verify"],
                "review": bands["review"],
                "escalation_rate": round(bands["review"] / total, 6),
            }
        )
    return series


def route_breakdown(db: Session, *, hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """Workflow run outcomes by route, joining in queue depth."""
    window = AnalyticsWindow.build(hours)
    runs = list(
        db.scalars(select(WorkflowRun).where(WorkflowRun.created_at >= window.start))
    )
    by_route = Counter(run.route or "unknown" for run in runs)
    by_status = Counter(run.status for run in runs)

    action_status = Counter()
    for run in runs:
        for action in run.actions or []:
            if isinstance(action, dict):
                action_status[f"{action.get('kind')}:{action.get('status')}"] += 1

    review_items = list(
        db.scalars(select(ReviewItem).where(ReviewItem.created_at >= window.start))
    )
    reviewed = [item for item in review_items if item.status in ("approved", "corrected")]
    corrections = [item for item in reviewed if item.status == "corrected"]

    return {
        "window": window.to_dict(),
        "runs": len(runs),
        "by_route": dict(by_route),
        "by_status": dict(by_status),
        "action_outcomes": dict(action_status),
        "review_items": len(review_items),
        "review_open": sum(1 for item in review_items if item.status in ("pending", "in_review")),
        "correction_rate": (
            round(len(corrections) / len(reviewed), 6) if reviewed else None
        ),
    }


def calibration_monitor(db: Session, *, hours: int = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """ECE over time plus the current reliability diagram."""
    window = AnalyticsWindow.build(hours)
    evaluations = [item for item in _evaluations(db, window) if item.label and item.confidence is not None]

    if not evaluations:
        return {
            "window": window.to_dict(),
            "n_samples": 0,
            "note": (
                "No labelled rows in this window. Label rows via an upload with a "
                "label column, the review queue, or the API."
            ),
        }

    confidences = [float(item.confidence) for item in evaluations]
    correct = [
        str(item.top_choice).strip().lower() == str(item.label).strip().lower()
        for item in evaluations
    ]
    report = calibration_report(confidences, correct)

    # ECE grouped by day, so drift is visible.
    daily: dict[str, tuple[list[float], list[bool]]] = {}
    for evaluation, outcome in zip(evaluations, correct, strict=True):
        key = _as_utc(evaluation.created_at).strftime("%Y-%m-%d")
        bucket = daily.setdefault(key, ([], []))
        bucket[0].append(float(evaluation.confidence))
        bucket[1].append(outcome)

    trend = []
    for key in sorted(daily):
        values, outcomes = daily[key]
        day_report = calibration_report(values, outcomes)
        trend.append(
            {
                "date": key,
                "n": day_report.n_samples,
                "ece": day_report.ece,
                "accuracy": day_report.accuracy,
                "mean_confidence": day_report.mean_confidence,
                "gap": (
                    None
                    if day_report.accuracy is None or day_report.mean_confidence is None
                    else round(day_report.mean_confidence - day_report.accuracy, 6)
                ),
            }
        )

    report_dict = report.to_dict()
    report_dict["trend"] = trend
    report_dict["window"] = window.to_dict()
    report_dict["interpretation"] = _interpret_calibration(report.ece, report.accuracy, report.mean_confidence)
    return report_dict


def _interpret_calibration(
    ece: float | None, accuracy: float | None, mean_confidence: float | None
) -> str:
    if ece is None or accuracy is None or mean_confidence is None:
        return "Not enough data to judge calibration."
    direction = "over" if mean_confidence > accuracy else "under"
    if ece < 0.05:
        return (
            f"Well calibrated (ECE {ece:.3f}). Stated percentages track observed accuracy — "
            "the confidence thresholds can be trusted as written."
        )
    if ece < 0.15:
        return (
            f"Moderately {direction}-confident (ECE {ece:.3f}: {mean_confidence:.1%} stated vs "
            f"{accuracy:.1%} observed). Refit the temperature before tightening thresholds."
        )
    return (
        f"Badly {direction}-confident (ECE {ece:.3f}: {mean_confidence:.1%} stated vs "
        f"{accuracy:.1%} observed). Confidence gating is unsafe until this is refit."
    )


def dashboard(db: Session, *, hours: int = DEFAULT_WINDOW_HOURS, bucket: str = "hour") -> dict[str, Any]:
    """Everything the analytics page needs in one round trip."""
    window = AnalyticsWindow.build(hours)
    evaluations = _evaluations(db, window)
    confidences = [item.confidence for item in evaluations if item.confidence is not None]

    return {
        "overview": overview(db, hours=hours),
        "histogram": confidence_histogram(confidences),
        "timeseries": timeseries(db, hours=hours, bucket=bucket),
        "routes": route_breakdown(db, hours=hours),
        "calibration": calibration_monitor(db, hours=hours),
        "thresholds": {
            "auto": get_settings().auto_threshold,
            "verify": get_settings().verify_threshold,
        },
    }
