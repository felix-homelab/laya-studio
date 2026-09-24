"""Part 1 — schema A/B testing."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ABTest
from ..schemas import ABTestCreate, ABTestDetail, ABTestSummary
from ..services import ab as ab_service
from ..services import decision_schemas

router = APIRouter(prefix="/api/ab", tags=["ab-testing"])


def _get_test(db: Session, test_id: int) -> ABTest:
    test = ab_service.get_test(db, test_id)
    if test is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No A/B test {test_id}.")
    return test


@router.post("/tests", response_model=ABTestDetail, status_code=status.HTTP_201_CREATED)
def create_test(payload: ABTestCreate, db: Session = Depends(get_db)) -> ABTestDetail:
    """Register an A/B test and run it immediately."""
    schema = decision_schemas.get_schema(db, payload.schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {payload.schema_id}.")

    revision_a = decision_schemas.get_revision(db, payload.revision_a_id)
    revision_b = decision_schemas.get_revision(db, payload.revision_b_id)
    if revision_a is None or revision_b is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "One of the revisions does not exist.")
    if revision_a.schema_id != schema.id or revision_b.schema_id != schema.id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Both revisions must belong to the selected schema.",
        )

    dataset = payload.dataset
    if dataset is None and payload.source_job_id is not None:
        dataset = ab_service.dataset_from_job(db, payload.source_job_id)
    if not dataset:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide `dataset` or a `source_job_id` with rows.",
        )

    try:
        test = ab_service.create_ab_test(
            db,
            name=payload.name,
            schema=schema,
            revision_a=revision_a,
            revision_b=revision_b,
            dataset=dataset,
        )
        ab_service.run_ab_test(db, test, model=payload.model)
    except ab_service.ABError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    db.refresh(test)
    return ABTestDetail.model_validate(test)


@router.get("/tests", response_model=list[ABTestSummary])
def list_tests(
    schema_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[ABTestSummary]:
    rows = ab_service.list_tests(db, schema_id=schema_id, limit=limit)
    return [ABTestSummary.model_validate(row) for row in rows]


@router.get("/tests/{test_id}", response_model=ABTestDetail)
def get_test(test_id: int, db: Session = Depends(get_db)) -> ABTestDetail:
    return ABTestDetail.model_validate(_get_test(db, test_id))


@router.post("/tests/{test_id}/run", response_model=ABTestDetail)
def rerun_test(test_id: int, db: Session = Depends(get_db)) -> ABTestDetail:
    test = _get_test(db, test_id)
    try:
        ab_service.run_ab_test(db, test)
    except ab_service.ABError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    db.refresh(test)
    return ABTestDetail.model_validate(test)


@router.delete("/tests/{test_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_test(test_id: int, db: Session = Depends(get_db)) -> None:
    db.delete(_get_test(db, test_id))
    db.commit()
