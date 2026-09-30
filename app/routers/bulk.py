"""Part 1 — bulk testing over uploaded CSV/JSON datasets."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import BulkJob, BulkJobItem
from ..schemas import BulkItemResponse, BulkJobDetail, BulkJobSummary
from ..services import bulk as bulk_service
from ..services import decision_schemas

router = APIRouter(prefix="/api/bulk", tags=["bulk"])


def _get_job(db: Session, job_id: int) -> BulkJob:
    job = db.get(BulkJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No bulk job {job_id}.")
    return job


#: Rows returned alongside a job by default, and the ceiling a caller may ask for.
DEFAULT_ITEM_LIMIT = 200
MAX_ITEM_LIMIT = 2000


def _detail(
    db: Session,
    job: BulkJob,
    *,
    include_items: bool = True,
    item_limit: int = DEFAULT_ITEM_LIMIT,
) -> BulkJobDetail:
    """A job's detail response, with its rows bounded by ``item_limit``.

    Built from the summary model rather than by validating ``BulkJobDetail`` against
    the ORM object: that reads the ``items`` relationship and loads every row of the
    job — up to ``MAX_BULK_ROWS`` of them — whatever ``item_limit`` said. Shared by the
    upload, fetch and rerun responses so all three bound their rows the same way.
    """
    detail = BulkJobDetail(**BulkJobSummary.model_validate(job).model_dump())
    if include_items:
        rows = db.scalars(
            select(BulkJobItem)
            .where(BulkJobItem.job_id == job.id)
            .order_by(BulkJobItem.row_index)
            .limit(item_limit)
        )
        detail.items = [BulkItemResponse.model_validate(row) for row in rows]
    return detail


@router.post("/upload", response_model=BulkJobDetail, status_code=status.HTTP_201_CREATED)
async def upload(
    file: Annotated[UploadFile, File(description="CSV or JSON dataset")],
    schema_id: Annotated[int, Form()],
    name: Annotated[str, Form()] = "",
    state_column: Annotated[str | None, Form()] = None,
    label_column: Annotated[str | None, Form()] = None,
    model: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> BulkJobDetail:
    """Upload a dataset, score every row, and return the metrics plus a preview."""
    settings = get_settings()
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")

    # Read one byte past the limit, so an oversized upload is rejected without
    # being pulled into memory whole.
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Upload exceeds {settings.max_upload_mb} MB "
            "(raise LAYA_STUDIO_MAX_UPLOAD_MB to allow larger files).",
        )

    def score() -> BulkJobDetail:
        dataset = bulk_service.parse_dataset(content, file.filename or "")
        job = bulk_service.create_job(
            db,
            name=name or (file.filename or "Bulk run"),
            dataset=dataset,
            filename=file.filename or "",
            schema=schema,
            state_column=state_column or None,
            label_column=label_column or None,
            model=model or None,
        )
        bulk_service.attach_rows(db, job, dataset)
        bulk_service.run_job(db, job)
        db.refresh(job)
        # Bounded like the fetch endpoint: a 5 000-row job must not answer with 5 000
        # items merely because it was uploaded rather than looked up.
        return _detail(db, job)

    # The handler is async only to stream the upload; scoring every row is
    # synchronous DB and model work, so it runs off the event loop.
    try:
        return await run_in_threadpool(score)
    except bulk_service.BulkError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("/jobs", response_model=list[BulkJobSummary])
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[BulkJobSummary]:
    rows = db.scalars(select(BulkJob).order_by(BulkJob.created_at.desc()).limit(limit))
    return [BulkJobSummary.model_validate(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=BulkJobDetail)
def get_job(
    job_id: int,
    include_items: bool = True,
    item_limit: int = Query(default=DEFAULT_ITEM_LIMIT, ge=1, le=MAX_ITEM_LIMIT),
    db: Session = Depends(get_db),
) -> BulkJobDetail:
    return _detail(db, _get_job(db, job_id), include_items=include_items, item_limit=item_limit)


@router.get("/jobs/{job_id}/items", response_model=list[BulkItemResponse])
def list_items(
    job_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=MAX_ITEM_LIMIT),
    db: Session = Depends(get_db),
) -> list[BulkItemResponse]:
    _get_job(db, job_id)
    rows = db.scalars(
        select(BulkJobItem)
        .where(BulkJobItem.job_id == job_id)
        .order_by(BulkJobItem.row_index)
        .offset(offset)
        .limit(limit)
    )
    return [BulkItemResponse.model_validate(row) for row in rows]


@router.post("/jobs/{job_id}/rerun", response_model=BulkJobDetail)
def rerun_job(job_id: int, db: Session = Depends(get_db)) -> BulkJobDetail:
    """Re-score every row, for example after a calibration refit."""
    job = _get_job(db, job_id)
    try:
        bulk_service.run_job(db, job)
    except bulk_service.BulkError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.refresh(job)
    return _detail(db, job)


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, db: Session = Depends(get_db)) -> None:
    job = _get_job(db, job_id)
    db.delete(job)
    db.commit()
