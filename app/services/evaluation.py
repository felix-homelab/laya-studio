"""The single path every decision takes through the system.

``run_evaluation`` is the one place that: lints a question bundle, calls an
engine, normalises the answer into the visualizer payload, optionally applies a
fitted temperature, and persists an immutable :class:`~app.models.Evaluation`.

Everything else — the playground, bulk uploads, A/B tests, webhook ingestion and
the workflow engine — is a thin wrapper over it. That keeps the audit trail
consistent no matter which door the state came through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..inference import (
    DecisionEngine,
    DecisionResult,
    build_question_results,
    flatten_state,
    get_engine,
    has_errors,
    validate_questions,
)
from ..inference.calibration import apply_platt_to_distribution
from ..models import EvalSchema, Evaluation
from . import calibration as calibration_service


class InvalidQuestions(ValueError):
    """The question bundle failed linting."""


@dataclass(slots=True)
class EvaluationOutcome:
    """An evaluation plus the context the caller needs to render it."""

    evaluation: Evaluation
    result: DecisionResult
    question_results: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    calibration: dict[str, Any] = field(default_factory=dict)

    @property
    def primary(self) -> dict[str, Any] | None:
        """The question result that drives routing."""
        if not self.question_results:
            return None
        name = self.evaluation.primary_question
        if name:
            for entry in self.question_results:
                if entry["question"] == name:
                    return entry
        return self.question_results[0]


def normalize_state(state: Any) -> dict[str, Any]:
    """Coerce any accepted state into the dict Laya takes."""
    if isinstance(state, dict):
        return {str(key): value for key, value in state.items()}
    if isinstance(state, str):
        return {"text": state}
    if isinstance(state, (list, tuple)):
        return {"text": "\n".join(str(item) for item in state)}
    raise ValueError(f"Unsupported state type: {type(state).__name__}")


def resolve_questions(schema: EvalSchema | None, questions: Any) -> dict[str, dict[str, Any]]:
    """Work out which question bundle to run, preferring an explicit override.

    Only ``None`` means "no override": an explicit empty bundle is linted (and
    rejected) rather than silently replaced by the schema's questions.
    """
    if questions is not None:
        if not isinstance(questions, dict):
            raise InvalidQuestions("`questions` must be an object keyed by question name.")
        candidate = {str(name): dict(definition) for name, definition in questions.items()}
    elif schema is not None:
        candidate = schema.questions
    else:
        raise InvalidQuestions("Provide either a schema or an inline `questions` bundle.")

    findings = validate_questions(candidate)
    if has_errors(findings):
        errors = "; ".join(
            f"{item['question'] or '(schema)'}: {item['message']}"
            for item in findings
            if item["severity"] == "error"
        )
        raise InvalidQuestions(errors)
    return candidate


def pick_primary_question(
    questions: dict[str, dict[str, Any]], requested: str | None = None
) -> str | None:
    """Decide which question is the routing signal.

    Order of preference: an explicit request, then the first ``choice`` question
    (most actionable), then the first ``noul``, then whatever comes first.
    """
    if not questions:
        return None
    if requested and requested in questions:
        return requested

    for primitive in ("choice", "noul", "score"):
        for name, definition in questions.items():
            if definition.get("type") == primitive:
                return name
    return next(iter(questions))


def _calibrate_entry(
    entry: dict[str, Any],
    active: calibration_service.ActiveCalibration,
) -> dict[str, Any]:
    """Apply the recalibration map to one question result, in place."""
    if active.is_identity:
        entry["calibrated"] = False
        entry["temperature"] = None
        entry["raw_confidence"] = entry.get("confidence")
        return entry

    fit = active.fit
    raw_confidence = entry.get("confidence")

    if raw_confidence is not None:
        entry["raw_confidence"] = raw_confidence
        entry["confidence"] = round(fit.apply(raw_confidence), 6)

    breakdown = entry.get("breakdown") or []
    if breakdown:
        # Score breakdowns are keyed by display label, which is unique per level.
        distribution = {item["label"]: item["probability"] for item in breakdown}
        rescaled = apply_platt_to_distribution(distribution, fit)
        ranked = sorted(rescaled.items(), key=lambda kv: kv[1], reverse=True)
        # Carry `raw_label` (Laya's own key, e.g. a score index) through the rescale
        # so the stored shape matches the uncalibrated path.
        raw_labels = {item["label"]: item.get("raw_label", item["label"]) for item in breakdown}
        entry["breakdown"] = [
            {
                "label": label,
                "raw_label": raw_labels.get(label, label),
                "probability": round(probability, 6),
                "rank": rank + 1,
                "is_top": rank == 0,
            }
            for rank, (label, probability) in enumerate(ranked)
        ]
        entry["alternatives"] = [item for item in entry["breakdown"] if not item["is_top"]]

        # The choice itself can flip under a softening transform only if two
        # options were within the temperature's noise floor; keep the argmax so
        # the record stays self-consistent with its own breakdown.
        top = entry["breakdown"][0] if entry["breakdown"] else None
        if top:
            if entry.get("type") == "choice":
                entry["choice"] = top["label"]
            elif entry.get("type") == "score":
                entry["level"] = top["label"]
            entry["confidence"] = top["probability"]
            if raw_confidence is not None:
                entry["raw_confidence"] = raw_confidence

    entry["calibrated"] = True
    entry["temperature"] = None if fit.temperature is None else round(fit.temperature, 6)
    entry["calibration_slope"] = round(fit.slope, 6)
    entry["calibration_intercept"] = round(fit.intercept, 6)
    return entry


def run_evaluation(
    db: Session,
    *,
    state: Any,
    questions: dict[str, dict[str, Any]] | None = None,
    schema: EvalSchema | None = None,
    model: str | None = None,
    source: str = "playground",
    primary_question: str | None = None,
    apply_calibration: bool = True,
    label: str | None = None,
    label_source: str | None = None,
    engine: DecisionEngine | None = None,
    persist: bool = True,
    commit: bool = True,
) -> EvaluationOutcome:
    """Evaluate one state and (by default) record it.

    Raises :class:`InvalidQuestions` for a malformed bundle and propagates engine
    errors, so callers can decide whether to 400 or to log and continue.
    """
    settings = get_settings()
    engine = engine or get_engine()

    resolved_questions = resolve_questions(schema, questions)
    state_dict = normalize_state(state)
    state_text = flatten_state(state_dict)
    primary = pick_primary_question(resolved_questions, primary_question)

    result = engine.predict(state_dict, resolved_questions, model=model)

    question_results = build_question_results(
        result.answers,
        resolved_questions,
        auto_threshold=settings.auto_threshold,
        verify_threshold=settings.verify_threshold,
    )

    warnings: list[str] = []
    calibration_meta: dict[str, Any] = {}

    if apply_calibration and settings.calibration_enabled:
        primary_active: calibration_service.ActiveCalibration | None = None
        for entry in question_results:
            active = calibration_service.get_active_calibration(
                db,
                schema_id=schema.id if schema is not None else None,
                question=entry["question"],
            )
            _calibrate_entry(entry, active)
            if entry["question"] == primary:
                calibration_meta = active.to_dict()
                primary_active = active
        # Only warn when a map was actually applied; a cached identity is a no-op.
        if primary_active is not None and not primary_active.is_identity:
            warnings.append(
                f"A calibration map was applied to '{primary}' over "
                f"{calibration_meta.get('n_samples', 0)} labelled rows: "
                f"{calibration_meta.get('note') or 'fitted'}"
            )
    else:
        for entry in question_results:
            entry["calibrated"] = False
            entry["temperature"] = None

    primary_entry = next(
        (entry for entry in question_results if entry["question"] == primary),
        question_results[0] if question_results else None,
    )

    top_choice: str | None = None
    confidence: float | None = None
    raw_confidence: float | None = None
    calibrated_flag = False
    if primary_entry is not None:
        top_choice = primary_entry.get("choice")
        if top_choice is None and primary_entry.get("type") == "score":
            top_choice = primary_entry.get("level") or _format_score(primary_entry.get("score"))
        if top_choice is None and primary_entry.get("type") == "noul":
            noul = primary_entry.get("noul")
            top_choice = None if noul is None else ("true" if noul >= 0.5 else "false")
        confidence = primary_entry.get("confidence")
        raw_confidence = primary_entry.get("raw_confidence", confidence)
        calibrated_flag = bool(primary_entry.get("calibrated"))

    evaluation = Evaluation(
        schema_id=schema.id if schema is not None else None,
        state=state_dict,
        state_text=state_text,
        questions=resolved_questions,
        answers=result.answers,
        question_results=question_results,
        routing=result.routing,
        engine=result.engine_name,
        engine_model=result.model,
        primary_question=primary,
        top_choice=top_choice,
        confidence=confidence,
        raw_confidence=raw_confidence,
        calibrated=calibrated_flag,
        latency_ms=result.latency_ms,
        source=source,
        label=label,
        label_source=label_source,
    )

    if not persist:
        return EvaluationOutcome(
            evaluation=evaluation,
            result=result,
            question_results=question_results,
            warnings=warnings,
            calibration=calibration_meta,
        )

    db.add(evaluation)
    if commit:
        db.commit()
        db.refresh(evaluation)
    else:
        db.flush()

    return EvaluationOutcome(
        evaluation=evaluation,
        result=result,
        question_results=question_results,
        warnings=warnings,
        calibration=calibration_meta,
    )


def _format_score(score: float | None) -> str | None:
    return None if score is None else f"{score:.2f}"


def label_evaluation(
    db: Session,
    evaluation: Evaluation,
    *,
    label: str,
    label_source: str = "human",
    commit: bool = True,
) -> Evaluation:
    """Attach ground truth, which is what makes calibration possible."""
    evaluation.label = label
    evaluation.label_source = label_source
    if commit:
        db.commit()
        db.refresh(evaluation)
    return evaluation


def get_evaluation(db: Session, evaluation_id: int) -> Evaluation | None:
    return db.get(Evaluation, evaluation_id)
