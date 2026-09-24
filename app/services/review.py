"""The manual review queue — the human half of the hybrid fallback.

Reviewing an item is not just a UI action: approving or correcting an item
writes the ground-truth label back onto the underlying evaluation, which is what
lets the calibration monitor refit a temperature. Review is how the system
learns, not just where work goes to wait.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import REVIEW_STATUS, Evaluation, ReviewItem, WorkflowRun

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("pending", "in_review")


def enqueue(
    db: Session,
    *,
    state: dict[str, Any],
    state_text: str = "",
    workflow_id: int | None = None,
    workflow_run_id: int | None = None,
    evaluation_id: int | None = None,
    question: str | None = None,
    model_choice: str | None = None,
    model_confidence: float | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    reason: str = "low_confidence",
    priority: int = 0,
    commit: bool = True,
) -> ReviewItem:
    """Add an item to the queue, de-duplicating on the source evaluation."""
    if evaluation_id is not None:
        existing = db.scalar(
            select(ReviewItem).where(
                ReviewItem.evaluation_id == evaluation_id,
                ReviewItem.status.in_(OPEN_STATUSES),
            )
        )
        if existing is not None:
            return existing

    item = ReviewItem(
        workflow_id=workflow_id,
        workflow_run_id=workflow_run_id,
        evaluation_id=evaluation_id,
        state=state,
        state_text=state_text,
        question=question,
        model_choice=model_choice,
        model_confidence=model_confidence,
        alternatives=alternatives or [],
        reason=reason,
        priority=priority,
        status="pending",
    )
    db.add(item)
    if commit:
        db.commit()
        db.refresh(item)
    else:
        db.flush()
    return item


def list_items(
    db: Session,
    *,
    status: str | None = None,
    workflow_id: int | None = None,
    reason: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ReviewItem]:
    statement = select(ReviewItem)
    if status == "open":
        statement = statement.where(ReviewItem.status.in_(OPEN_STATUSES))
    elif status:
        statement = statement.where(ReviewItem.status == status)
    if workflow_id is not None:
        statement = statement.where(ReviewItem.workflow_id == workflow_id)
    if reason:
        statement = statement.where(ReviewItem.reason == reason)
    if search:
        statement = statement.where(ReviewItem.state_text.ilike(f"%{search.strip()}%"))

    statement = statement.order_by(
        ReviewItem.priority.desc(),
        ReviewItem.created_at.asc(),
    )
    return list(db.scalars(statement.limit(limit).offset(offset)))


def get_item(db: Session, item_id: int) -> ReviewItem | None:
    return db.get(ReviewItem, item_id)


def claim(db: Session, item: ReviewItem, *, assignee: str, commit: bool = True) -> ReviewItem:
    item.assignee = assignee
    item.status = "in_review"
    if commit:
        db.commit()
        db.refresh(item)
    return item


def resolve(
    db: Session,
    item: ReviewItem,
    *,
    status: str,
    corrected_label: str | None = None,
    notes: str = "",
    assignee: str | None = None,
    commit: bool = True,
) -> ReviewItem:
    """Close an item out, writing ground truth back onto the evaluation.

    * ``approved``  — the model was right; the model's choice becomes the label.
    * ``corrected`` — the model was wrong; the correction becomes the label.
    * ``rejected``  — the item was not a valid decision; no label is written.

    Writing the label is what feeds the calibration monitor, so it happens here
    rather than in the router.
    """
    if status not in REVIEW_STATUS:
        raise ValueError(f"Unknown review status {status!r}; expected one of {', '.join(REVIEW_STATUS)}.")
    if status == "corrected" and not (corrected_label or "").strip():
        raise ValueError("A corrected item requires a corrected_label.")

    item.status = status
    item.notes = notes
    item.resolved_at = datetime.now(timezone.utc)
    if assignee:
        item.assignee = assignee

    label: str | None = None
    if status == "approved":
        label = item.model_choice
        item.corrected_label = item.model_choice
    elif status == "corrected":
        label = corrected_label.strip() if corrected_label else None
        item.corrected_label = label

    if label and item.evaluation_id:
        evaluation = db.get(Evaluation, item.evaluation_id)
        if evaluation is not None:
            evaluation.label = label
            evaluation.label_source = "review"

    if commit:
        db.commit()
        db.refresh(item)
    return item


def stats(db: Session) -> dict[str, Any]:
    """Queue health for the review workspace header."""
    by_status = {
        status: int(count)
        for status, count in db.execute(
            select(ReviewItem.status, func.count(ReviewItem.id)).group_by(ReviewItem.status)
        )
    }
    by_reason = {
        reason: int(count)
        for reason, count in db.execute(
            select(ReviewItem.reason, func.count(ReviewItem.id)).group_by(ReviewItem.reason)
        )
    }

    open_items = list(
        db.scalars(select(ReviewItem).where(ReviewItem.status.in_(OPEN_STATUSES)).limit(500))
    )
    confidences = [
        item.model_confidence for item in open_items if item.model_confidence is not None
    ]

    resolved = sum(
        count for status, count in by_status.items() if status in ("approved", "corrected", "rejected")
    )
    corrected = by_status.get("corrected", 0)
    reviewed = by_status.get("approved", 0) + corrected

    return {
        "total": sum(by_status.values()),
        "open": sum(by_status.get(status, 0) for status in OPEN_STATUSES),
        "by_status": by_status,
        "by_reason": by_reason,
        "resolved": resolved,
        "reviewed": reviewed,
        "correction_rate": (corrected / reviewed) if reviewed else None,
        "mean_open_confidence": (
            round(sum(confidences) / len(confidences), 6) if confidences else None
        ),
        "oldest_open": min((item.created_at for item in open_items), default=None),
    }


def pending_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count(ReviewItem.id)).where(ReviewItem.status.in_(OPEN_STATUSES))
        )
        or 0
    )


def run_review_url(run: WorkflowRun) -> str | None:
    """Deep link used by the workflow visualizer."""
    return f"/review?run={run.id}" if run.id else None
