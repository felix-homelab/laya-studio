"""ORM -> wire-format conversion shared by the routers."""

from __future__ import annotations

from typing import Any

from .models import Evaluation, WorkflowRun
from .schemas import (
    EvaluationResponse,
    QuestionResult,
    WorkflowRunDetail,
)


def question_results(payload: list[dict[str, Any]] | None) -> list[QuestionResult]:
    output: list[QuestionResult] = []
    for entry in payload or []:
        if not isinstance(entry, dict):
            continue
        output.append(
            QuestionResult(
                question=str(entry.get("question", "")),
                type=str(entry.get("type", "choice")),
                instructions=str(entry.get("instructions", "")),
                choice=entry.get("choice"),
                level=entry.get("level"),
                confidence=entry.get("confidence"),
                raw_confidence=entry.get("raw_confidence"),
                calibrated=bool(entry.get("calibrated", False)),
                temperature=entry.get("temperature"),
                calibration_slope=entry.get("calibration_slope"),
                calibration_intercept=entry.get("calibration_intercept"),
                noul=entry.get("noul"),
                score=entry.get("score"),
                max_score=entry.get("max_score"),
                act_probability=entry.get("act_probability"),
                rubric=list(entry.get("rubric") or []),
                breakdown=list(entry.get("breakdown") or []),
                alternatives=list(entry.get("alternatives") or []),
                band=str(entry.get("band", "unknown")),
            )
        )
    return output


def to_evaluation_response(
    evaluation: Evaluation,
    *,
    warnings: list[str] | None = None,
    calibration: dict[str, Any] | None = None,
) -> EvaluationResponse:
    """Serialise a persisted :class:`~app.models.Evaluation`."""
    return EvaluationResponse(
        id=evaluation.id,
        schema_id=evaluation.schema_id,
        state=evaluation.state or {},
        state_text=evaluation.state_text or "",
        question_results=question_results(evaluation.question_results),
        routing=evaluation.routing or {},
        engine=evaluation.engine,
        engine_model=evaluation.engine_model,
        primary_question=evaluation.primary_question,
        top_choice=evaluation.top_choice,
        confidence=evaluation.confidence,
        raw_confidence=evaluation.raw_confidence,
        calibrated=evaluation.calibrated,
        latency_ms=evaluation.latency_ms,
        source=evaluation.source,
        label=evaluation.label,
        created_at=evaluation.created_at,
        warnings=warnings or [],
        calibration=calibration or {},
    )


def to_run_detail(
    run: WorkflowRun,
    *,
    evaluation: Evaluation | None = None,
) -> WorkflowRunDetail:
    detail = WorkflowRunDetail.model_validate(run)
    if evaluation is not None:
        detail.evaluation = to_evaluation_response(evaluation)
    return detail
