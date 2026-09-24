"""Part 1 — the interactive decision & query playground."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..inference import get_engine, validate_questions
from ..models import EvalSchema, Evaluation
from ..schemas import (
    EvaluationRequest,
    EvaluationResponse,
    LabelRequest,
    LintRequest,
    LintResponse,
    RouteRequest,
    RouteResponse,
)
from ..serializers import to_evaluation_response
from ..services import decision_schemas
from ..services import evaluation as evaluation_service

router = APIRouter(prefix="/api/playground", tags=["playground"])


def _resolve_schema(db: Session, schema_id: int | None, schema_key: str | None) -> EvalSchema | None:
    if schema_id is not None:
        schema = decision_schemas.get_schema(db, schema_id)
        if schema is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema with id {schema_id}.")
        return schema
    if schema_key:
        schema = decision_schemas.get_schema_by_key(db, schema_key)
        if schema is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema with key {schema_key!r}.")
        return schema
    return None


@router.post("/evaluate", response_model=EvaluationResponse)
def evaluate(
    payload: EvaluationRequest,
    db: Session = Depends(get_db),
) -> EvaluationResponse:
    """Score one state. This is the endpoint behind the playground form."""
    schema = _resolve_schema(db, payload.schema_id, payload.schema_key)
    questions = (
        {name: definition.model_dump(exclude_none=True) for name, definition in payload.questions.items()}
        if payload.questions
        else None
    )

    if schema is None and questions is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Provide either `schema_id`/`schema_key` or an inline `questions` bundle.",
        )

    try:
        outcome = evaluation_service.run_evaluation(
            db,
            state=payload.state,
            questions=questions,
            schema=schema,
            model=payload.model,
            source="playground",
            primary_question=payload.primary_question,
            apply_calibration=payload.apply_calibration,
            persist=payload.persist,
        )
    except evaluation_service.InvalidQuestions as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return to_evaluation_response(
        outcome.evaluation,
        warnings=outcome.warnings,
        calibration=outcome.calibration,
    )


@router.post("/route", response_model=RouteResponse)
def route(payload: RouteRequest) -> RouteResponse:
    """Explain which checkpoint would serve a state, without scoring it."""
    engine = get_engine()
    return RouteResponse(routing=engine.route(payload.state))


@router.post("/lint", response_model=LintResponse)
def lint(payload: LintRequest) -> LintResponse:
    """Lint a question bundle — used live by the no-code schema editor."""
    findings = validate_questions(dict(payload.questions))
    return LintResponse(
        findings=[
            {"severity": item["severity"], "question": item["question"], "message": item["message"]}
            for item in findings
        ],
        ok=not any(item["severity"] == "error" for item in findings),
    )


@router.get("/history", response_model=list[EvaluationResponse])
def history(
    limit: int = Query(default=25, ge=1, le=200),
    schema_id: int | None = None,
    db: Session = Depends(get_db),
) -> list[EvaluationResponse]:
    statement = select(Evaluation).where(Evaluation.source == "playground")
    if schema_id is not None:
        statement = statement.where(Evaluation.schema_id == schema_id)
    rows = db.scalars(statement.order_by(Evaluation.created_at.desc()).limit(limit))
    return [to_evaluation_response(row) for row in rows]


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationResponse)
def get_evaluation(evaluation_id: int, db: Session = Depends(get_db)) -> EvaluationResponse:
    evaluation = evaluation_service.get_evaluation(db, evaluation_id)
    if evaluation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No evaluation {evaluation_id}.")
    return to_evaluation_response(evaluation)


@router.post("/evaluations/{evaluation_id}/label", response_model=EvaluationResponse)
def label_evaluation(
    evaluation_id: int,
    payload: LabelRequest,
    db: Session = Depends(get_db),
) -> EvaluationResponse:
    """Attach ground truth so the row counts toward calibration."""
    evaluation = evaluation_service.get_evaluation(db, evaluation_id)
    if evaluation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No evaluation {evaluation_id}.")
    evaluation_service.label_evaluation(
        db, evaluation, label=payload.label, label_source=payload.label_source
    )
    return to_evaluation_response(evaluation)
