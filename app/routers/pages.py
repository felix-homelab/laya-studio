"""Server-rendered pages.

Each page ships an HTML shell with the data it needs for its first paint; live
updates then go through the JSON API in ``app/static/js/``. That keeps the
initial load instant without introducing a build step.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import ab as ab_service
from ..services import analytics as analytics_service
from ..services import calibration as calibration_service
from ..services import decision_schemas, review, workflows
from ..templating import page_context, templates

router = APIRouter(tags=["pages"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def playground_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    schemas = decision_schemas.list_schemas(db)
    return templates.TemplateResponse(
        request,
        "playground.html",
        page_context(
            request,
            active="Playground",
            schemas=schemas,
            history=[],
        ),
    )


@router.get("/schemas", response_class=HTMLResponse, include_in_schema=False)
def schemas_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = decision_schemas.list_schemas(db)
    return templates.TemplateResponse(
        request,
        "schemas.html",
        page_context(request, active="Schemas", schemas=rows),
    )


@router.get("/bulk", response_class=HTMLResponse, include_in_schema=False)
def bulk_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    from sqlalchemy import select

    from ..models import BulkJob

    jobs = list(db.scalars(select(BulkJob).order_by(BulkJob.created_at.desc()).limit(25)))
    return templates.TemplateResponse(
        request,
        "bulk.html",
        page_context(
            request,
            active="Bulk testing",
            schemas=decision_schemas.list_schemas(db),
            jobs=jobs,
        ),
    )


@router.get("/ab", response_class=HTMLResponse, include_in_schema=False)
def ab_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    tests = ab_service.list_tests(db, limit=25)
    return templates.TemplateResponse(
        request,
        "ab.html",
        page_context(
            request,
            active="A/B testing",
            schemas=decision_schemas.list_schemas(db),
            tests=tests,
        ),
    )


@router.get("/workflows", response_class=HTMLResponse, include_in_schema=False)
def workflows_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    rows = workflows.list_workflows(db)
    runs = workflows.list_runs(db, limit=25)
    return templates.TemplateResponse(
        request,
        "workflows.html",
        page_context(
            request,
            active="Workflows",
            workflows=rows,
            runs=runs,
            stats=workflows.workflow_summary(db),
            schemas=decision_schemas.list_schemas(db, include_templates=False),
        ),
    )


@router.get("/analytics", response_class=HTMLResponse, include_in_schema=False)
def analytics_page(
    request: Request,
    hours: int = 24 * 7,
    bucket: str = "hour",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if bucket not in ("hour", "day"):
        bucket = "hour"
    # The same range the /api/analytics endpoints accept.
    hours = max(1, min(hours, 24 * 365))
    return templates.TemplateResponse(
        request,
        "analytics.html",
        page_context(
            request,
            active="Analytics",
            hours=hours,
            bucket=bucket,
            data=analytics_service.dashboard(db, hours=hours, bucket=bucket),
            snapshots=calibration_service.latest_snapshots(db),
        ),
    )


@router.get("/review", response_class=HTMLResponse, include_in_schema=False)
def review_page(
    request: Request,
    status: str | None = "open",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    items = review.list_items(db, status=status, limit=200)
    return templates.TemplateResponse(
        request,
        "review.html",
        page_context(
            request,
            active="Review queue",
            items=items,
            stats=review.stats(db),
            status_filter=status or "all",
        ),
    )
