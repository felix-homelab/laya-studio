"""Part 4 — real-time analytics and the calibration monitor."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import CalibrationRefitResponse
from ..serializers import snapshot_to_dict
from ..services import analytics as analytics_service
from ..services import calibration as calibration_service

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


def _validate_bucket(bucket: str) -> str:
    if bucket not in ("hour", "day"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "bucket must be 'hour' or 'day'."
        )
    return bucket


@router.get("/dashboard")
def dashboard(
    hours: int = Query(default=24 * 7, ge=1, le=24 * 365),
    bucket: str = "hour",
    db: Session = Depends(get_db),
) -> dict:
    """Everything the analytics page renders, in one request."""
    return analytics_service.dashboard(db, hours=hours, bucket=_validate_bucket(bucket))


@router.get("/overview")
def overview(
    hours: int = Query(default=24 * 7, ge=1, le=24 * 365),
    db: Session = Depends(get_db),
) -> dict:
    return analytics_service.overview(db, hours=hours)


@router.get("/timeseries")
def timeseries(
    hours: int = Query(default=24 * 7, ge=1, le=24 * 365),
    bucket: str = "hour",
    db: Session = Depends(get_db),
) -> list[dict]:
    return analytics_service.timeseries(db, hours=hours, bucket=_validate_bucket(bucket))


@router.get("/routes")
def routes(
    hours: int = Query(default=24 * 7, ge=1, le=24 * 365),
    db: Session = Depends(get_db),
) -> dict:
    return analytics_service.route_breakdown(db, hours=hours)


@router.get("/calibration")
def calibration(
    hours: int = Query(default=24 * 7, ge=1, le=24 * 365),
    db: Session = Depends(get_db),
) -> dict:
    """ECE, Brier, the reliability diagram and the ECE-per-day trend."""
    report = analytics_service.calibration_monitor(db, hours=hours)
    report["snapshots"] = [
        snapshot_to_dict(snapshot) for snapshot in calibration_service.latest_snapshots(db)
    ]
    return report


@router.get("/calibration/report")
def calibration_report_slice(
    schema_id: int | None = None,
    question: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """On-demand reliability report for one schema/question slice."""
    return calibration_service.report_for(db, schema_id=schema_id, question=question).to_dict()


@router.post("/calibration/refit", response_model=CalibrationRefitResponse)
def refit(db: Session = Depends(get_db)) -> CalibrationRefitResponse:
    """Refit a temperature for every slice that has enough labelled rows."""
    snapshots = calibration_service.refresh_all(db)
    if not snapshots:
        from ..config import get_settings

        return CalibrationRefitResponse(
            snapshots=[],
            message=(
                "Nothing to refit: no (schema, question) slice has at least "
                f"{get_settings().calibration_min_samples} labelled evaluations yet."
            ),
        )
    return CalibrationRefitResponse(
        snapshots=[snapshot_to_dict(snapshot) for snapshot in snapshots],
        message=f"Refit {len(snapshots)} slice(s).",
    )


@router.get("/thresholds")
def thresholds() -> dict:
    """The live act-vs-escalate ladder, for the UI's threshold markers."""
    from ..config import get_settings

    settings = get_settings()
    return {
        "auto": settings.auto_threshold,
        "verify": settings.verify_threshold,
        "calibration_enabled": settings.calibration_enabled,
        "calibration_min_samples": settings.calibration_min_samples,
    }
