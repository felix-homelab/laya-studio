"""Part 4 — the manual review queue."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import ReviewClaimRequest, ReviewItemResponse, ReviewResolveRequest
from ..services import review as review_service
from ..services import workflows as workflow_service

router = APIRouter(prefix="/api/review", tags=["review"])


def _get_item(db: Session, item_id: int):
    item = review_service.get_item(db, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No review item {item_id}.")
    return item


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    return review_service.stats(db)


@router.get("/items", response_model=list[ReviewItemResponse])
def list_items(
    status_filter: str | None = Query(default=None, alias="status"),
    workflow_id: int | None = None,
    reason: str | None = None,
    search: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[ReviewItemResponse]:
    rows = review_service.list_items(
        db,
        status=status_filter,
        workflow_id=workflow_id,
        reason=reason,
        search=search,
        limit=limit,
        offset=offset,
    )
    return [ReviewItemResponse.model_validate(row) for row in rows]


@router.get("/items/{item_id}", response_model=ReviewItemResponse)
def get_item(item_id: int, db: Session = Depends(get_db)) -> ReviewItemResponse:
    return ReviewItemResponse.model_validate(_get_item(db, item_id))


@router.post("/items/{item_id}/claim", response_model=ReviewItemResponse)
def claim(
    item_id: int,
    payload: ReviewClaimRequest,
    db: Session = Depends(get_db),
) -> ReviewItemResponse:
    item = review_service.claim(db, _get_item(db, item_id), assignee=payload.assignee)
    return ReviewItemResponse.model_validate(item)


@router.post("/items/{item_id}/resolve", response_model=ReviewItemResponse)
def resolve(
    item_id: int,
    payload: ReviewResolveRequest,
    db: Session = Depends(get_db),
) -> ReviewItemResponse:
    """Close an item out. Approving or correcting writes a ground-truth label."""
    item = _get_item(db, item_id)
    try:
        item = review_service.resolve(
            db,
            item,
            status=payload.status,
            corrected_label=payload.corrected_label,
            notes=payload.notes,
            assignee=payload.assignee,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return ReviewItemResponse.model_validate(item)


@router.post("/items/{item_id}/retry", response_model=dict)
def retry_workflow(item_id: int, db: Session = Depends(get_db)) -> dict:
    """Re-run the owning workflow against the same state after a correction."""
    item = _get_item(db, item_id)
    if item.workflow_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This item is not attached to a workflow."
        )
    workflow = workflow_service.get_workflow(db, item.workflow_id)
    if workflow is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The owning workflow no longer exists.")

    outcome = workflow_service.run_workflow(
        db,
        workflow,
        state=item.state,
        source="workflow",
        payload={"retry_of_review_item": item.id},
    )
    return {
        "run_id": outcome.run.id,
        "route": outcome.route,
        "effective_route": outcome.effective_route,
        "actions": outcome.actions,
        "error": outcome.error,
    }
