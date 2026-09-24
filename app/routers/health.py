"""Health and self-description."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..inference import engine_status
from ..llm.service import get_provider
from ..models import (
    ABTest,
    BulkJob,
    CalibrationSnapshot,
    EvalSchema,
    Evaluation,
    ReviewItem,
    Workflow,
    WorkflowRun,
)

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Liveness plus engine state.

    Always 200 while the process is up — including while Laya is still loading,
    because the panel *is* serving. Use ``/api/ready`` to gate traffic on the
    engine actually being usable.
    """
    settings = get_settings()
    status = engine_status()
    provider = get_provider(settings)

    counts = {
        "schemas": EvalSchema,
        "evaluations": Evaluation,
        "bulk_jobs": BulkJob,
        "ab_tests": ABTest,
        "workflows": Workflow,
        "workflow_runs": WorkflowRun,
        "review_items": ReviewItem,
        "calibration_snapshots": CalibrationSnapshot,
    }
    tables: dict[str, int] = {}
    for name, model in counts.items():
        tables[name] = int(db.scalar(select(func.count(model.id))) or 0)

    return {
        # "ok" once the engine is usable, "starting" while it loads, "degraded"
        # if it cannot be built at all.
        "status": "ok" if status["ready"] else ("starting" if status["state"] != "failed" else "degraded"),
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "engine": status,
        "llm": {
            "provider": settings.llm_provider,
            "enabled": settings.llm_enabled,
            "model": provider.describe().get("model") if provider else None,
        },
        "thresholds": {
            "auto": settings.auto_threshold,
            "verify": settings.verify_threshold,
        },
        "database": {
            "dialect": db.bind.dialect.name if db.bind is not None else "unknown",
            "reachable": True,
            "tables": tables,
        },
    }


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """Readiness: 503 until the engine can actually serve a decision.

    Kubernetes/Compose readiness probes should use this rather than ``/health``,
    otherwise they will route traffic to a replica that answers every evaluation
    with a 503.
    """
    from ..db import ping as db_ping

    status = engine_status()
    database = db_ping(db.bind)

    if status["ready"] and database:
        return JSONResponse(
            status_code=200,
            content={"ready": True, "engine": status, "database": True},
        )
    return JSONResponse(
        status_code=503,
        content={"ready": False, "engine": status, "database": database},
        headers={"Retry-After": "10"},
    )
