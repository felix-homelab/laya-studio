"""The workflow automation engine.

The confidence ladder from the brief, made concrete::

    confidence >= auto_threshold    -> auto    : run the automatic actions
    confidence >= verify_threshold  -> verify  : ask a generative model to adjudicate,
                                                 then auto-execute or escalate
    otherwise                       -> review  : park it in the human queue

Two decisions worth calling out:

* **Verification failure is not a green light.** If the LLM is unconfigured,
  times out, or returns something unparseable, the item escalates to human
  review rather than proceeding on the model's say-so.
* **Every path is logged.** Each action produces an
  :class:`~app.models.ActionLog` row, so the visualizer can show what fired and
  what came back without re-deriving it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..llm.service import VerificationVerdict, verify_decision
from ..models import EvalSchema, Evaluation, Workflow, WorkflowRun
from . import actions as action_service
from . import evaluation as evaluation_service

logger = logging.getLogger(__name__)

ROUTE_ORDER = ("auto", "verify", "review")
STEP_TARGETS = ("auto", "verify", "review", "always")

SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Used when a workflow has no steps configured, so the ladder is visible from
#: the first run instead of silently doing nothing.
DEFAULT_STEPS: list[dict[str, Any]] = [
    {
        "on": "auto",
        "kind": "noop",
        "config": {"detail": "Auto-executed {choice} at {confidence} confidence."},
    },
    {
        "on": "verify",
        "kind": "noop",
        "config": {"detail": "Verified {choice} via the generative fallback."},
    },
    {
        "on": "review",
        "kind": "review",
        "config": {"reason": "low_confidence"},
    },
]


class WorkflowError(ValueError):
    """Raised for an unusable workflow configuration."""


@dataclass(slots=True)
class WorkflowOutcome:
    """Everything a caller (or the visualizer) needs about one run."""

    run: WorkflowRun
    evaluation: Evaluation | None
    route: str
    effective_route: str
    verification: VerificationVerdict | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return SLUG_RE.sub("-", ascii_only).strip("-") or "workflow"


def unique_key(db: Session, base: str) -> str:
    candidate = slugify(base)
    suffix = 2
    while db.scalar(select(Workflow.id).where(Workflow.key == candidate)) is not None:
        candidate = f"{slugify(base)}-{suffix}"
        suffix += 1
    return candidate


def decide_route(
    confidence: float | None,
    auto_threshold: float,
    verify_threshold: float,
) -> str:
    """Map a confidence onto ``auto`` / ``verify`` / ``review``.

    A missing confidence is treated as *no signal*, which escalates rather than
    auto-executes.
    """
    if confidence is None:
        return "review"
    if confidence >= auto_threshold:
        return "auto"
    if confidence >= verify_threshold:
        return "verify"
    return "review"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_workflows(db: Session, *, enabled_only: bool = False) -> list[Workflow]:
    statement = select(Workflow).order_by(Workflow.name.asc())
    if enabled_only:
        statement = statement.where(Workflow.enabled.is_(True))
    return list(db.scalars(statement))


def get_workflow(db: Session, workflow_id: int) -> Workflow | None:
    return db.get(Workflow, workflow_id)


def get_workflow_by_key(db: Session, key: str) -> Workflow | None:
    return db.scalar(select(Workflow).where(Workflow.key == key))


def resolve_workflow(db: Session, identifier: int | str) -> Workflow | None:
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        return get_workflow(db, int(identifier))
    return get_workflow_by_key(db, slugify(str(identifier))) or get_workflow_by_key(db, str(identifier))


def create_workflow(
    db: Session,
    *,
    name: str,
    schema: EvalSchema | None = None,
    description: str = "",
    trigger_type: str = "api",
    trigger_config: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    decision_question: str | None = None,
    auto_threshold: float | None = None,
    verify_threshold: float | None = None,
    enabled: bool = True,
    key: str | None = None,
    commit: bool = True,
) -> Workflow:
    settings = get_settings()
    resolved_steps = steps if steps is not None else []

    for index, step in enumerate(resolved_steps):
        if not isinstance(step, dict):
            raise WorkflowError(f"Step {index} must be an object.")
        target = str(step.get("on", "auto"))
        if target not in STEP_TARGETS:
            raise WorkflowError(
                f"Step {index} has on={target!r}; expected one of {', '.join(STEP_TARGETS)}."
            )
        if str(step.get("kind", "noop")) not in action_service.SUPPORTED_ACTIONS:
            raise WorkflowError(
                f"Step {index} has kind={step.get('kind')!r}; expected one of "
                f"{', '.join(action_service.SUPPORTED_ACTIONS)}."
            )

    resolved_auto = auto_threshold if auto_threshold is not None else settings.auto_threshold
    resolved_verify = (
        verify_threshold if verify_threshold is not None else settings.verify_threshold
    )
    # The ladder must stay monotone: a verify threshold above the auto threshold
    # would make the middle band unreachable.
    if resolved_verify > resolved_auto:
        resolved_verify = resolved_auto

    workflow = Workflow(
        key=key or unique_key(db, name),
        name=name.strip() or "Untitled workflow",
        description=description.strip(),
        enabled=enabled,
        trigger_type=trigger_type,
        trigger_config=trigger_config or {},
        schema_id=schema.id if schema is not None else None,
        decision_question=decision_question,
        steps=resolved_steps,
        auto_threshold=resolved_auto,
        verify_threshold=resolved_verify,
    )
    db.add(workflow)
    if commit:
        db.commit()
        db.refresh(workflow)
    else:
        db.flush()
    return workflow


def update_workflow(
    db: Session,
    workflow: Workflow,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    trigger_type: str | None = None,
    trigger_config: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    schema: EvalSchema | None = None,
    decision_question: str | None = None,
    auto_threshold: float | None = None,
    verify_threshold: float | None = None,
    commit: bool = True,
) -> Workflow:
    if name is not None:
        workflow.name = name.strip() or workflow.name
    if description is not None:
        workflow.description = description.strip()
    if enabled is not None:
        workflow.enabled = enabled
    if trigger_type is not None:
        workflow.trigger_type = trigger_type
    if trigger_config is not None:
        workflow.trigger_config = trigger_config
    if steps is not None:
        workflow.steps = steps
    if schema is not None:
        workflow.schema_id = schema.id
    if decision_question is not None:
        workflow.decision_question = decision_question or None
    if auto_threshold is not None:
        workflow.auto_threshold = auto_threshold
    if verify_threshold is not None:
        workflow.verify_threshold = verify_threshold

    if workflow.verify_threshold > workflow.auto_threshold:
        workflow.verify_threshold = workflow.auto_threshold

    if commit:
        db.commit()
        db.refresh(workflow)
    return workflow


def delete_workflow(db: Session, workflow: Workflow, *, commit: bool = True) -> None:
    db.delete(workflow)
    if commit:
        db.commit()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_workflow(
    db: Session,
    workflow: Workflow,
    *,
    state: Any,
    source: str = "workflow",
    payload: dict[str, Any] | None = None,
    model: str | None = None,
    commit: bool = True,
) -> WorkflowOutcome:
    """Evaluate a state and drive it down the confidence ladder."""
    settings = get_settings()

    run = WorkflowRun(
        workflow_id=workflow.id,
        status="running",
        trigger_payload=payload or {},
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()

    if not workflow.enabled:
        return _fail(db, run, "This workflow is disabled.", commit=commit)

    schema = db.get(EvalSchema, workflow.schema_id) if workflow.schema_id else None
    if schema is None:
        return _fail(db, run, "This workflow has no schema attached.", commit=commit)

    try:
        outcome = evaluation_service.run_evaluation(
            db,
            state=state,
            schema=schema,
            model=model,
            source=source,
            primary_question=workflow.decision_question,
            commit=False,
        )
    except Exception as exc:  # noqa: BLE001 - record and surface, don't 500
        logger.exception("Workflow %s evaluation failed", workflow.key)
        return _fail(db, run, f"{type(exc).__name__}: {exc}", commit=commit)

    evaluation = outcome.evaluation
    run.evaluation_id = evaluation.id

    primary = outcome.primary or {}
    question = str(primary.get("question") or workflow.decision_question or "")
    choice = evaluation.top_choice
    confidence = evaluation.confidence
    alternatives = primary.get("alternatives") or []

    run.confidence = confidence
    route = decide_route(confidence, workflow.auto_threshold, workflow.verify_threshold)
    run.route = route
    run.final_choice = choice

    verification: VerificationVerdict | None = None
    effective_route = route

    if route == "verify":
        verification = _verify(
            db, run, workflow, schema, evaluation, question, choice, confidence, primary, commit=False
        )
        if verification.status == "ok" and verification.choice:
            run.final_choice = verification.choice
            effective_route = "auto" if verification.agrees else "review"
        else:
            # Could not verify -> escalate rather than auto-execute.
            effective_route = "review"

    steps = workflow.steps or DEFAULT_STEPS
    context = action_service.build_context(
        workflow=workflow,
        evaluation=evaluation,
        route=route,
        question=question,
        choice=run.final_choice,
        confidence=confidence,
        state_text=evaluation.state_text,
        reason=(
            "verification_disagreement"
            if verification is not None and verification.status == "ok" and not verification.agrees
            else ("verification_unavailable" if verification is not None and verification.status != "ok" else "low_confidence")
        ),
        alternatives=alternatives,
        run=run,
        extra={
            "effective_route": effective_route,
            "verification": verification.to_dict() if verification else None,
        },
    )

    executed: list[dict[str, Any]] = []
    for step in steps:
        target = str(step.get("on", "auto"))
        if target not in ("always", effective_route):
            continue
        result = action_service.execute(
            db,
            kind=str(step.get("kind", "noop")),
            config=step.get("config") or {},
            context=context,
            settings=settings,
            commit=False,
        )
        action_service.log_outcome(db, result, run=run, commit=False)
        executed.append(result.to_dict())

    if not executed:
        # Nothing matched: record that explicitly so the run is never a silent no-op.
        result = action_service.execute(
            db,
            kind="noop",
            config={"detail": f"No step configured for route '{effective_route}'."},
            context=context,
            settings=settings,
            commit=False,
        )
        action_service.log_outcome(db, result, run=run, commit=False)
        executed.append(result.to_dict())

    run.actions = executed
    run.status = "succeeded"
    run.finished_at = datetime.now(timezone.utc)
    db.flush()

    if commit:
        db.commit()
        db.refresh(run)

    return WorkflowOutcome(
        run=run,
        evaluation=evaluation,
        route=route,
        effective_route=effective_route,
        verification=verification,
        actions=executed,
        error=outcome.warnings[0] if outcome.warnings else None,
    )


def _verify(
    db: Session,
    run: WorkflowRun,
    workflow: Workflow,
    schema: EvalSchema,
    evaluation: Evaluation,
    question: str,
    choice: str | None,
    confidence: float | None,
    primary: dict[str, Any],
    *,
    commit: bool,
) -> VerificationVerdict:
    """Run the generative fallback and log it like any other action."""
    definition = (schema.questions or {}).get(question, {})
    candidates = [item["label"] for item in (primary.get("breakdown") or [])]
    if not candidates:
        criteria = definition.get("criteria")
        if isinstance(criteria, dict):
            candidates = [str(label) for label in criteria]
        elif isinstance(criteria, list):
            candidates = [str(level) for level in criteria]

    verdict = verify_decision(
        question=question or "decision",
        instructions=str(definition.get("instructions", "")),
        state_text=evaluation.state_text,
        candidates=candidates,
        candidate_choice=choice,
        candidate_confidence=confidence,
    )

    outcome = action_service.ActionOutcome(
        kind="llm_verify",
        status="ok" if verdict.status == "ok" else ("failed" if verdict.status == "failed" else "skipped"),
        target=str(verdict.model or verdict.provider or ""),
        detail=verdict.rationale or (verdict.error or ""),
        request={
            "question": question,
            "candidate_choice": choice,
            "candidate_confidence": confidence,
            "candidates": candidates,
        },
        response=verdict.to_dict(),
        error=None if verdict.status == "ok" else verdict.error,
        latency_ms=verdict.latency_ms,
    )
    action_service.log_outcome(db, outcome, run=run, commit=commit)
    return verdict


def _fail(
    db: Session,
    run: WorkflowRun,
    message: str,
    *,
    commit: bool,
) -> WorkflowOutcome:
    run.status = "failed"
    run.route = "error"
    run.error = message
    run.finished_at = datetime.now(timezone.utc)
    db.flush()
    if commit:
        db.commit()
        db.refresh(run)
    return WorkflowOutcome(
        run=run,
        evaluation=None,
        route="error",
        effective_route="error",
        error=message,
    )


def list_runs(
    db: Session,
    *,
    workflow_id: int | None = None,
    route: str | None = None,
    limit: int = 100,
) -> list[WorkflowRun]:
    statement = select(WorkflowRun)
    if workflow_id is not None:
        statement = statement.where(WorkflowRun.workflow_id == workflow_id)
    if route:
        statement = statement.where(WorkflowRun.route == route)
    return list(db.scalars(statement.order_by(WorkflowRun.created_at.desc()).limit(limit)))


def get_run(db: Session, run_id: int) -> WorkflowRun | None:
    return db.get(WorkflowRun, run_id)


def workflow_summary(db: Session) -> dict[str, Any]:
    """Counts used by the workflow list page."""
    from sqlalchemy import func

    total = int(db.scalar(select(func.count(Workflow.id))) or 0)
    enabled = int(
        db.scalar(select(func.count(Workflow.id)).where(Workflow.enabled.is_(True))) or 0
    )
    runs = {
        route: int(count)
        for route, count in db.execute(
            select(WorkflowRun.route, func.count(WorkflowRun.id)).group_by(WorkflowRun.route)
        )
    }
    return {"total": total, "enabled": enabled, "runs_by_route": runs}
