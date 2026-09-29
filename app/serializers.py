"""ORM -> wire-format conversion shared by the routers."""

from __future__ import annotations

from typing import Any

from .models import CalibrationSnapshot, Evaluation, WorkflowRun
from .schemas import (
    EvaluationResponse,
    QuestionResult,
    WorkflowRunDetail,
)


def _as_list(value: Any) -> list[Any]:
    """A JSONB list field, or empty when the stored value is not a list."""
    return list(value) if isinstance(value, list) else []


def question_results(payload: list[dict[str, Any]] | None) -> list[QuestionResult]:
    output: list[QuestionResult] = []
    for entry in payload or []:
        if not isinstance(entry, dict):
            continue
        output.append(
            QuestionResult(
                # `or`, not a `.get` default: a stored null must not become "None".
                question=str(entry.get("question") or ""),
                type=str(entry.get("type") or "choice"),
                instructions=str(entry.get("instructions") or ""),
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
                rubric=_as_list(entry.get("rubric")),
                breakdown=_as_list(entry.get("breakdown")),
                alternatives=_as_list(entry.get("alternatives")),
                band=str(entry.get("band") or "unknown"),
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


def snapshot_to_dict(snapshot: CalibrationSnapshot) -> dict[str, Any]:
    """One calibration fit, as the analytics endpoints return it."""
    return {
        "id": snapshot.id,
        "primitive": snapshot.primitive,
        "question": snapshot.question,
        "schema_id": snapshot.schema_id,
        "n_samples": snapshot.n_samples,
        "slope": snapshot.slope,
        "intercept": snapshot.intercept,
        "temperature": snapshot.temperature,
        "ece": snapshot.ece,
        "mce": snapshot.mce,
        "brier": snapshot.brier,
        "accuracy": snapshot.accuracy,
        "mean_confidence": snapshot.mean_confidence,
        "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
    }
