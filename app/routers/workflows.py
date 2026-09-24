"""Part 2 — workflow automation: definitions, runs and the visualizer feed."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Workflow
from ..schemas import (
    EvaluationResponse,
    IngestRequest,
    WorkflowCreate,
    WorkflowRunDetail,
    WorkflowRunSummary,
    WorkflowSummary,
    WorkflowUpdate,
)
from ..serializers import to_evaluation_response
from ..services import evaluation as evaluation_service
from ..services import workflows as workflow_service

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


def _get_workflow(db: Session, workflow_id: int) -> Workflow:
    workflow = workflow_service.get_workflow(db, workflow_id)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No workflow {workflow_id}.")
    return workflow


@router.get("", response_model=list[WorkflowSummary])
def list_workflows(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
) -> list[WorkflowSummary]:
    return [
        WorkflowSummary.model_validate(row)
        for row in workflow_service.list_workflows(db, enabled_only=enabled_only)
    ]


@router.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict:
    return workflow_service.workflow_summary(db)


@router.post("", response_model=WorkflowSummary, status_code=status.HTTP_201_CREATED)
def create_workflow(payload: WorkflowCreate, db: Session = Depends(get_db)) -> WorkflowSummary:
    from ..services import decision_schemas

    schema = None
    if payload.schema_id is not None:
        schema = decision_schemas.get_schema(db, payload.schema_id)
        if schema is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {payload.schema_id}.")

    try:
        workflow = workflow_service.create_workflow(
            db,
            name=payload.name,
            description=payload.description,
            schema=schema,
            trigger_type=payload.trigger_type,
            trigger_config=payload.trigger_config,
            steps=[step.model_dump() for step in payload.steps],
            decision_question=payload.decision_question,
            auto_threshold=payload.auto_threshold,
            verify_threshold=payload.verify_threshold,
            enabled=payload.enabled,
            key=payload.key,
        )
    except workflow_service.WorkflowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return WorkflowSummary.model_validate(workflow)


@router.get("/runs", response_model=list[WorkflowRunSummary])
def list_all_runs(
    route: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[WorkflowRunSummary]:
    return [
        WorkflowRunSummary.model_validate(row)
        for row in workflow_service.list_runs(db, route=route, limit=limit)
    ]


# NOTE: the /runs/* routes are declared before /{workflow_id} so that path
# matching never has to guess whether "runs" is a workflow id.
@router.get("/runs/{run_id}", response_model=WorkflowRunDetail)
def get_run(run_id: int, db: Session = Depends(get_db)) -> WorkflowRunDetail:
    run = workflow_service.get_run(db, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No workflow run {run_id}.")
    detail = WorkflowRunDetail.model_validate(run)
    if run.evaluation_id:
        evaluation = evaluation_service.get_evaluation(db, run.evaluation_id)
        if evaluation is not None:
            detail.evaluation = to_evaluation_response(evaluation)
    return detail


@router.get("/runs/{run_id}/evaluation", response_model=EvaluationResponse)
def get_run_evaluation(run_id: int, db: Session = Depends(get_db)) -> EvaluationResponse:
    run = workflow_service.get_run(db, run_id)
    if run is None or run.evaluation_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This run has no evaluation attached.")
    evaluation = evaluation_service.get_evaluation(db, run.evaluation_id)
    if evaluation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "The evaluation no longer exists.")
    return to_evaluation_response(evaluation)


@router.get("/{workflow_id}", response_model=WorkflowSummary)
def get_workflow(workflow_id: int, db: Session = Depends(get_db)) -> WorkflowSummary:
    return WorkflowSummary.model_validate(_get_workflow(db, workflow_id))


@router.put("/{workflow_id}", response_model=WorkflowSummary)
def update_workflow(
    workflow_id: int,
    payload: WorkflowUpdate,
    db: Session = Depends(get_db),
) -> WorkflowSummary:
    from ..services import decision_schemas

    workflow = _get_workflow(db, workflow_id)

    schema = None
    if payload.schema_id is not None:
        schema = decision_schemas.get_schema(db, payload.schema_id)
        if schema is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {payload.schema_id}.")

    try:
        workflow = workflow_service.update_workflow(
            db,
            workflow,
            name=payload.name,
            description=payload.description,
            enabled=payload.enabled,
            trigger_type=payload.trigger_type,
            trigger_config=payload.trigger_config,
            steps=[step.model_dump() for step in payload.steps] if payload.steps is not None else None,
            schema=schema,
            decision_question=payload.decision_question,
            auto_threshold=payload.auto_threshold,
            verify_threshold=payload.verify_threshold,
        )
    except workflow_service.WorkflowError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return WorkflowSummary.model_validate(workflow)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)) -> None:
    workflow_service.delete_workflow(db, _get_workflow(db, workflow_id))


@router.post("/{workflow_id}/run")
def run_workflow(
    workflow_id: int,
    payload: IngestRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Manually fire a workflow — the ``manual`` trigger."""
    workflow = _get_workflow(db, workflow_id)
    try:
        state = payload.resolve_state()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    outcome = workflow_service.run_workflow(
        db,
        workflow,
        state=state,
        source="workflow",
        payload={"manual": True, **payload.metadata},
    )
    return {
        "run": WorkflowRunSummary.model_validate(outcome.run).model_dump(),
        "route": outcome.route,
        "effective_route": outcome.effective_route,
        "verification": outcome.verification.to_dict() if outcome.verification else None,
        "actions": outcome.actions,
        "evaluation": (
            to_evaluation_response(outcome.evaluation).model_dump()
            if outcome.evaluation is not None
            else None
        ),
        "error": outcome.error,
    }


@router.get("/{workflow_id}/runs", response_model=list[WorkflowRunSummary])
def list_runs(
    workflow_id: int,
    route: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[WorkflowRunSummary]:
    _get_workflow(db, workflow_id)
    return [
        WorkflowRunSummary.model_validate(row)
        for row in workflow_service.list_runs(db, workflow_id=workflow_id, route=route, limit=limit)
    ]
