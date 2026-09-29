"""Schema A/B testing.

The brief's question: *does tweaking wording or a choice taxonomy shift the
model's probabilities?* Answering it needs two things this module provides:

1. The **same dataset** evaluated against two immutable schema revisions.
2. Metrics that separate *useful* change from *noise* — flip rate, confidence
   shift, and per-label transitions, not just "accuracy went up".

A high flip rate on unchanged labels is the signature of an under-specified
question: the model was never really reading the distinction, it was guessing.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..inference.base import EngineNotReady, EngineUnavailable
from ..models import ABTest, ABTestItem, EvalSchema, SchemaRevision
from . import evaluation as evaluation_service

logger = logging.getLogger(__name__)

MAX_AB_ROWS = 2000


class ABError(ValueError):
    """Raised for an invalid A/B configuration."""


def create_ab_test(
    db: Session,
    *,
    name: str,
    schema: EvalSchema,
    revision_a: SchemaRevision,
    revision_b: SchemaRevision,
    dataset: list[dict[str, Any]],
    commit: bool = True,
) -> ABTest:
    """Register an A/B test over two revisions of one schema.

    The model is not part of the test record; pass it to :func:`run_ab_test`.
    """
    if not dataset:
        raise ABError("An A/B test needs at least one row of test input.")
    if len(dataset) > MAX_AB_ROWS:
        raise ABError(f"{len(dataset)} rows exceeds the {MAX_AB_ROWS}-row limit for one A/B test.")
    if revision_a.id == revision_b.id:
        raise ABError("Pick two different revisions to compare.")

    test = ABTest(
        name=name.strip() or f"{schema.name}: r{revision_a.revision} vs r{revision_b.revision}",
        schema_id=schema.id,
        revision_a_id=revision_a.id,
        revision_b_id=revision_b.id,
        status="pending",
        dataset=dataset,
    )
    db.add(test)
    if commit:
        db.commit()
        db.refresh(test)
    else:
        db.flush()
    return test


def run_ab_test(db: Session, test: ABTest, *, model: str | None = None, commit: bool = True) -> ABTest:
    """Evaluate the dataset under both revisions and compute the deltas."""
    revision_a = db.get(SchemaRevision, test.revision_a_id) if test.revision_a_id else None
    revision_b = db.get(SchemaRevision, test.revision_b_id) if test.revision_b_id else None
    if revision_a is None or revision_b is None:
        raise ABError("One of the revisions attached to this test no longer exists.")

    db.query(ABTestItem).filter(ABTestItem.test_id == test.id).delete()
    db.flush()

    question_set_a = dict(revision_a.definition.get("questions", {}))
    question_set_b = dict(revision_b.definition.get("questions", {}))

    items: list[ABTestItem] = []
    skipped = 0
    for index, row in enumerate(test.dataset):
        state = row.get("state") if isinstance(row, dict) else None
        if state is None:
            state = {key: value for key, value in row.items() if key != "label"} if isinstance(row, dict) else {"text": str(row)}
        label = row.get("label") if isinstance(row, dict) else None

        try:
            # A SAVEPOINT per row: a row that fails mid-flush is rolled back on its
            # own instead of leaving the session unusable for the rows after it.
            with db.begin_nested():
                outcome_a = evaluation_service.run_evaluation(
                    db,
                    state=state,
                    questions=question_set_a,
                    schema=None,
                    model=model,
                    source="ab_test",
                    label=label,
                    label_source="dataset" if label else None,
                    commit=False,
                )
                outcome_b = evaluation_service.run_evaluation(
                    db,
                    state=state,
                    questions=question_set_b,
                    schema=None,
                    model=model,
                    source="ab_test",
                    label=label,
                    label_source="dataset" if label else None,
                    commit=False,
                )
        except (EngineNotReady, EngineUnavailable) as exc:
            test.status = "failed"
            test.error = str(exc)
            test.finished_at = datetime.now(timezone.utc)
            db.flush()
            if commit:
                db.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - skip a bad row, keep the test going
            logger.warning("A/B test %s row %s failed: %s", test.id, index, exc)
            skipped += 1
            continue

        choice_a = outcome_a.evaluation.top_choice
        choice_b = outcome_b.evaluation.top_choice

        items.append(
            ABTestItem(
                test_id=test.id,
                row_index=index,
                state=state,
                label=label,
                choice_a=choice_a,
                choice_b=choice_b,
                confidence_a=outcome_a.evaluation.confidence,
                confidence_b=outcome_b.evaluation.confidence,
                flipped=(choice_a or "").strip().lower() != (choice_b or "").strip().lower(),
                result_a={
                    "question": outcome_a.evaluation.primary_question,
                    "choice": choice_a,
                    "confidence": outcome_a.evaluation.confidence,
                    "breakdown": (outcome_a.primary or {}).get("breakdown", []),
                },
                result_b={
                    "question": outcome_b.evaluation.primary_question,
                    "choice": choice_b,
                    "confidence": outcome_b.evaluation.confidence,
                    "breakdown": (outcome_b.primary or {}).get("breakdown", []),
                },
            )
        )
        # No flush per row: the next row's evaluation (or the final commit) flushes it.
        db.add(items[-1])

    test.status = "succeeded" if items else "failed"
    test.summary = summarize_ab(items, revision_a, revision_b, skipped=skipped)
    test.finished_at = datetime.now(timezone.utc)

    if commit:
        db.commit()
        db.refresh(test)
    return test


def summarize_ab(
    items: list[ABTestItem],
    revision_a: SchemaRevision | None = None,
    revision_b: SchemaRevision | None = None,
    *,
    skipped: int = 0,
) -> dict[str, Any]:
    """Metrics that distinguish a real improvement from probability noise.

    ``skipped`` counts dataset rows that failed to evaluate and are not in ``items``.
    """
    if not items:
        return {"total": 0, "skipped": skipped, "note": "No rows were evaluated."}

    total = len(items)
    flips = [item for item in items if item.flipped]

    # Means and deltas over the same paired rows, so mean_b - mean_a == mean delta.
    paired = [
        item for item in items if item.confidence_a is not None and item.confidence_b is not None
    ]
    confidence_a = [item.confidence_a for item in paired]
    confidence_b = [item.confidence_b for item in paired]
    deltas = [item.confidence_b - item.confidence_a for item in paired]

    summary: dict[str, Any] = {
        "total": total,
        "skipped": skipped,
        "flip_count": len(flips),
        "flip_rate": len(flips) / total,
        "agreement": 1.0 - (len(flips) / total),
        "mean_confidence_a": round(sum(confidence_a) / len(confidence_a), 6) if confidence_a else None,
        "mean_confidence_b": round(sum(confidence_b) / len(confidence_b), 6) if confidence_b else None,
        "mean_confidence_delta": round(sum(deltas) / len(deltas), 6) if deltas else None,
        "confidence_up": sum(1 for delta in deltas if delta > 0.01),
        "confidence_down": sum(1 for delta in deltas if delta < -0.01),
        "transitions": _transitions(items),
        "revision_a": revision_a.revision if revision_a else None,
        "revision_b": revision_b.revision if revision_b else None,
    }

    labelled = [item for item in items if item.label]
    if labelled:
        accuracy_a = _accuracy(labelled, "a")
        accuracy_b = _accuracy(labelled, "b")
        summary["labelled"] = len(labelled)
        summary["accuracy_a"] = accuracy_a
        summary["accuracy_b"] = accuracy_b
        if accuracy_a is not None and accuracy_b is not None:
            summary["accuracy_delta"] = round(accuracy_b - accuracy_a, 6)
        # Flip rate restricted to rows where we know the truth and only one
        # revision is right — the flips that actually matter.
        corrected = sum(
            1
            for item in labelled
            if _is_correct(item, "a") is False and _is_correct(item, "b") is True
        )
        regressed = sum(
            1
            for item in labelled
            if _is_correct(item, "a") is True and _is_correct(item, "b") is False
        )
        summary["fixed_by_b"] = corrected
        summary["broken_by_b"] = regressed
        summary["net_gain"] = corrected - regressed

    if summary["flip_rate"] > 0.30:
        summary["interpretation"] = (
            f"{summary['flip_rate']:.0%} of rows changed answer. That is a lot of churn for a "
            "wording change — the distinction was probably under-specified to begin with."
        )
    elif summary["flip_rate"] == 0:
        summary["interpretation"] = (
            "No answers changed. The two revisions are behaviourally identical on this dataset, "
            "so any accuracy difference you see elsewhere is sampling noise."
        )
    else:
        summary["interpretation"] = (
            f"{summary['flip_rate']:.0%} of rows changed answer; check the transitions below "
            "for whether the movement is in a sensible direction."
        )

    return summary


def _transitions(items: list[ABTestItem]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter(
        (item.choice_a or "(none)", item.choice_b or "(none)") for item in items if item.flipped
    )
    return [
        {"from": source, "to": target, "count": count}
        for (source, target), count in counts.most_common(20)
    ]


def _is_correct(item: ABTestItem, side: str) -> bool | None:
    choice = item.choice_a if side == "a" else item.choice_b
    if not item.label or not choice:
        return None
    return item.label.strip().lower() == choice.strip().lower()


def _accuracy(items: list[ABTestItem], side: str) -> float | None:
    outcomes = [_is_correct(item, side) for item in items]
    known = [outcome for outcome in outcomes if outcome is not None]
    if not known:
        return None
    return round(sum(1 for outcome in known if outcome) / len(known), 6)


def list_tests(db: Session, *, schema_id: int | None = None, limit: int = 50) -> list[ABTest]:
    statement = select(ABTest)
    if schema_id is not None:
        statement = statement.where(ABTest.schema_id == schema_id)
    return list(db.scalars(statement.order_by(ABTest.created_at.desc()).limit(limit)))


def get_test(db: Session, test_id: int) -> ABTest | None:
    return db.get(ABTest, test_id)


def dataset_from_job(db: Session, job_id: int, limit: int = MAX_AB_ROWS) -> list[dict[str, Any]]:
    """Reuse a bulk job's rows as an A/B dataset."""
    from ..models import BulkJobItem

    rows = list(
        db.scalars(
            select(BulkJobItem)
            .where(BulkJobItem.job_id == job_id)
            .order_by(BulkJobItem.row_index)
            .limit(limit)
        )
    )
    return [{"state": row.state, "label": row.label} for row in rows]


def dataset_from_texts(texts: list[str]) -> list[dict[str, Any]]:
    return [{"state": {"text": text}} for text in texts if str(text).strip()]
