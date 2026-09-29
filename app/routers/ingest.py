"""Part 2 — multichannel triggers.

Ingestion is the boundary where untrusted text enters the system, so this router
is the one place with an auth check (:func:`~app.deps.require_ingest_token`) and
an explicit dry-run mode for wiring up a caller before going live.

Three doors are provided, matching the brief's "webhooks, API endpoints, or email
ingestion workers":

* ``POST /api/ingest/{workflow_key}``        — generic JSON or plain text
* ``POST /api/ingest/{workflow_key}/text``   — ``text/plain`` body for dumb senders
* ``POST /api/ingest/email/{workflow_key}``  — a parsed inbound email
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import require_ingest_token
from ..schemas import EmailIngestRequest, IngestRequest, WorkflowRunSummary
from ..serializers import to_evaluation_response
from ..services import evaluation as evaluation_service
from ..services import workflows as workflow_service

# The token check applies to every ingestion route at once.
router = APIRouter(
    prefix="/api/ingest",
    tags=["ingestion"],
    dependencies=[Depends(require_ingest_token)],
)


def _resolve(db: Session, workflow_key: str):
    workflow = workflow_service.resolve_workflow(db, workflow_key)
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No workflow {workflow_key!r}.")
    return workflow


def _respond(outcome, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "run": None if outcome.run is None else WorkflowRunSummary.model_validate(outcome.run).model_dump(),
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


def dry_run(db: Session, workflow, state: dict[str, Any] | str, *, source: str) -> dict[str, Any]:
    """Evaluate and show the routing decision without executing any actions.

    Shared with the manual trigger in :mod:`app.routers.workflows`, so a dry run
    means the same thing on every door.
    """
    schema = None
    if workflow.schema_id:
        from ..models import EvalSchema

        schema = db.get(EvalSchema, workflow.schema_id)
    try:
        outcome_eval = evaluation_service.run_evaluation(
            db,
            state=state,
            schema=schema,
            source=source,
            primary_question=workflow.decision_question,
        )
    except evaluation_service.InvalidQuestions as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    confidence = outcome_eval.evaluation.confidence
    route = workflow_service.decide_route(
        confidence, workflow.auto_threshold, workflow.verify_threshold
    )
    return {
        "dry_run": True,
        "route": route,
        "effective_route": route,
        "verification": None,
        "actions": [],
        "run": None,
        "evaluation": to_evaluation_response(outcome_eval.evaluation).model_dump(),
        "error": None,
    }


@router.post("/email/{workflow_key}", summary="Ingest a parsed inbound email")
def ingest_email(
    workflow_key: str,
    payload: EmailIngestRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    workflow = _resolve(db, workflow_key)
    state = {
        "from": payload.from_,
        "to": payload.to,
        "subject": payload.subject,
        "body": payload.body,
    }
    outcome = workflow_service.run_workflow(
        db,
        workflow,
        state={key: value for key, value in state.items() if value},
        source="email",
        # The channel marker goes last so caller metadata cannot overwrite it.
        payload={**payload.metadata, "channel": "email"},
    )
    return _respond(outcome, dry_run=False)


@router.post("/{workflow_key}/text")
async def ingest_text(
    workflow_key: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Accept a raw ``text/plain`` body — for senders that cannot emit JSON."""
    workflow = _resolve(db, workflow_key)
    body = (await request.body()).decode("utf-8", errors="replace").strip()
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The request body is empty.")

    # Async only to read the raw body; the run itself is synchronous DB, model
    # and action work, so it runs off the event loop.
    def run() -> dict[str, Any]:
        outcome = workflow_service.run_workflow(
            db,
            workflow,
            state={"text": body},
            source="api",
            payload={"channel": "text"},
        )
        return _respond(outcome, dry_run=False)

    return await run_in_threadpool(run)


@router.post("/{workflow_key}")
def ingest(
    workflow_key: str,
    payload: IngestRequest = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The general-purpose trigger: JSON ``state``, or plain ``text``."""
    workflow = _resolve(db, workflow_key)
    try:
        state = payload.resolve_state()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    if payload.dry_run:
        return dry_run(db, workflow, state, source="api")

    outcome = workflow_service.run_workflow(
        db,
        workflow,
        state=state,
        source="api",
        payload={**payload.metadata, "channel": "api"},
    )
    return _respond(outcome, dry_run=False)
