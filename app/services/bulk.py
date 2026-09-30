"""Bulk evaluation: CSV/JSON uploads scored row-by-row against one schema.

This is the "preview model performance" loop from the brief — drop in a
spreadsheet of test inputs, optionally with ground-truth labels, and get back
accuracy, mean confidence and ECE alongside the per-row predictions.

The job runs synchronously. For a few thousand rows against Laya that is fine
(the model batches internally); larger corpora should move to the worker-backed
path described in ``docs/ROADMAP.md``.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..inference.base import EngineNotReady, EngineUnavailable, flatten_state
from ..inference.calibration import calibration_report
from ..models import BulkJob, BulkJobItem, EvalSchema
from . import evaluation as evaluation_service

logger = logging.getLogger(__name__)

#: Hard ceiling on rows per upload — keeps memory bounded and the request fast.
MAX_BULK_ROWS = 5000


class BulkError(ValueError):
    """Raised for unparseable or oversized uploads."""


@dataclass(slots=True)
class ParsedDataset:
    rows: list[dict[str, Any]]
    columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    format: str = "csv"


def _decode(content: bytes) -> str:
    try:
        # utf-8-sig also decodes plain UTF-8, just stripping a leading BOM.
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Latin-1 maps every byte, so this terminal fallback cannot fail.
        return content.decode("latin-1")


def parse_dataset(content: bytes, filename: str = "") -> ParsedDataset:
    """Parse a CSV or JSON upload into a list of row dicts."""
    if not content:
        raise BulkError("The uploaded file is empty.")

    text = _decode(content).strip()
    if not text:
        raise BulkError("The uploaded file contains only whitespace.")

    lower_name = filename.lower()
    looks_like_json = lower_name.endswith(".json") or text[0] in "[{"

    if looks_like_json:
        dataset = _parse_json(text)
    else:
        dataset = _parse_csv(text)

    if not dataset.rows:
        raise BulkError("No data rows found in the upload.")

    if len(dataset.rows) > MAX_BULK_ROWS:
        raise BulkError(
            f"{len(dataset.rows)} rows exceeds the {MAX_BULK_ROWS}-row limit for one job. "
            "Split the file or use the API in batches."
        )

    return dataset


def _parse_csv(text: str) -> ParsedDataset:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise BulkError("The CSV has no header row.")

    columns = [str(name).strip() for name in reader.fieldnames if name]
    rows: list[dict[str, Any]] = []
    for raw in reader:
        row: dict[str, Any] = {}
        for key, value in raw.items():
            if key is None:
                continue
            row[str(key).strip()] = "" if value is None else str(value).strip()
        if any(str(value).strip() for value in row.values()):
            rows.append(row)

    return ParsedDataset(rows=rows, columns=columns, format="csv")


def _parse_json(text: str) -> ParsedDataset:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BulkError(f"Invalid JSON: {exc.msg} (line {exc.lineno}).") from exc

    if isinstance(payload, dict):
        for key in ("rows", "items", "data", "records", "states"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                payload = candidate
                break
        else:
            # A single object is a one-row dataset.
            payload = [payload]

    if not isinstance(payload, list):
        raise BulkError("JSON uploads must be a list of objects, or an object with a 'rows' key.")

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, item in enumerate(payload):
        if isinstance(item, dict):
            rows.append({str(key): value for key, value in item.items()})
        elif isinstance(item, str):
            rows.append({"text": item})
        else:
            warnings.append(f"Row {index} was {type(item).__name__} and was skipped.")

    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    return ParsedDataset(rows=rows, columns=columns, warnings=warnings, format="json")


def build_state(
    row: dict[str, Any],
    *,
    state_column: str | None = None,
    label_column: str | None = None,
) -> dict[str, Any]:
    """Turn one dataset row into the state handed to Laya.

    With ``state_column`` set, that single column becomes the free text; the
    remaining columns become a structured state so the model sees (and can be
    routed on) all available context.
    """
    if state_column and state_column in row:
        value = row[state_column]
        if isinstance(value, str):
            return {"text": value}
        if isinstance(value, dict):
            return {str(key): item for key, item in value.items()}
        return {"text": str(value)}

    state: dict[str, Any] = {}
    for key, value in row.items():
        if key == label_column or value is None or value == "":
            continue
        state[key] = value
    return state or {"text": ""}


def create_job(
    db: Session,
    *,
    name: str,
    dataset: ParsedDataset,
    filename: str = "",
    schema: EvalSchema | None = None,
    state_column: str | None = None,
    label_column: str | None = None,
    model: str | None = None,
    commit: bool = True,
) -> BulkJob:
    """Create a bulk job record in ``pending`` state.

    Takes the *parsed* dataset rather than the raw upload. Every caller needs the
    parsed rows for :func:`attach_rows` anyway, so the bytes were only ever here to be
    parsed a second time — decoding and parsing a multi-megabyte upload twice per job.
    """
    warnings = list(dataset.warnings)
    if state_column and state_column not in dataset.columns:
        # build_state falls back to every column; say so rather than silently
        # handing the model a wider state than the caller asked for.
        warnings.append(
            f"State column {state_column!r} is not in the upload; every column is used as the state."
        )

    job = BulkJob(
        name=name.strip() or filename or "Bulk run",
        schema_id=schema.id if schema is not None else None,
        filename=filename,
        status="pending",
        total_rows=len(dataset.rows),
        options={
            "state_column": state_column,
            "label_column": label_column,
            "model": model,
            "columns": dataset.columns,
            "format": dataset.format,
            "parse_warnings": warnings,
        },
    )
    db.add(job)
    if commit:
        db.commit()
        db.refresh(job)
    else:
        db.flush()
    return job


def run_job(db: Session, job: BulkJob, *, commit: bool = True) -> BulkJob:
    """Score every row of a job and write the summary metrics."""
    if job.status == "running":
        raise BulkError("This job is already running.")
    # The "running" status is only committed with the results, so the check above
    # cannot see a concurrent run. Lock the job row for the rest of this
    # transaction instead; SKIP LOCKED returns nothing if another run holds it.
    if db.scalar(select(BulkJob.id).where(BulkJob.id == job.id).with_for_update(skip_locked=True)) is None:
        raise BulkError("This job is already running.")
    if not job.schema_id:
        raise BulkError("This job has no schema attached.")

    schema = db.get(EvalSchema, job.schema_id)
    if schema is None:
        raise BulkError("The schema attached to this job no longer exists.")

    # Re-parse from the stored rows is not possible (we never stored the file),
    # so the rows are carried on the job options at creation time in the API
    # layer. Here we read the already-parsed items if present.
    items = list(
        db.scalars(select(BulkJobItem).where(BulkJobItem.job_id == job.id).order_by(BulkJobItem.row_index))
    )
    if not items:
        raise BulkError("No rows are attached to this job.")

    model = (job.options or {}).get("model")

    job.status = "running"
    job.started_at = datetime.now(timezone.utc)
    job.processed_rows = 0
    job.failed_rows = 0
    db.flush()

    for item in items:
        try:
            # A SAVEPOINT per row: a row that fails mid-flush is rolled back on its
            # own instead of leaving the session unusable for the rows after it.
            with db.begin_nested():
                outcome = evaluation_service.run_evaluation(
                    db,
                    state=item.state,
                    schema=schema,
                    model=model,
                    source="bulk",
                    label=item.label,
                    label_source="dataset" if item.label else None,
                    commit=False,
                )
            item.evaluation_id = outcome.evaluation.id
            item.predicted = outcome.evaluation.top_choice
            item.confidence = outcome.evaluation.confidence
            item.ok = True
            item.error = None
            job.processed_rows += 1
        except (EngineNotReady, EngineUnavailable) as exc:
            # Not a per-row problem: nothing in this batch can succeed. Failing
            # every row individually would produce a job that reports "0 rows
            # scored" with no hint as to why, so abort and say so.
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = datetime.now(timezone.utc)
            db.flush()
            if commit:
                db.commit()
            raise
        except Exception as exc:  # noqa: BLE001 - one bad row must not kill the job
            logger.warning("Bulk job %s row %s failed: %s", job.id, item.row_index, exc)
            item.ok = False
            item.error = f"{type(exc).__name__}: {exc}"[:500]
            job.failed_rows += 1
        db.flush()

    db.flush()
    job.summary = summarize(db, job)
    # A job "succeeds" as long as at least one row was scored; failed rows are
    # reported in `failed_rows` and surfaced in the summary rather than flipping
    # the whole job red.
    job.status = "succeeded" if job.processed_rows > 0 else "failed"
    job.finished_at = datetime.now(timezone.utc)

    if commit:
        db.commit()
        db.refresh(job)
    return job


def attach_rows(
    db: Session,
    job: BulkJob,
    dataset: ParsedDataset,
    *,
    commit: bool = True,
) -> list[BulkJobItem]:
    """Persist parsed rows as job items before running."""
    options = job.options or {}
    label_column = options.get("label_column")

    items: list[BulkJobItem] = []
    for index, row in enumerate(dataset.rows):
        label = None
        if label_column and label_column in row:
            raw = row[label_column]
            label = str(raw).strip() or None
        items.append(
            BulkJobItem(
                job_id=job.id,
                row_index=index,
                state=_state_for(row, options),
                label=label,
            )
        )
    db.add_all(items)
    if commit:
        db.commit()
    else:
        db.flush()
    return items


def _state_for(row: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    return build_state(
        row,
        state_column=options.get("state_column"),
        label_column=options.get("label_column"),
    )


def summarize(db: Session, job: BulkJob) -> dict[str, Any]:
    """Metrics for a completed job: accuracy, confidence, ECE, distributions."""
    items = list(
        db.scalars(select(BulkJobItem).where(BulkJobItem.job_id == job.id).order_by(BulkJobItem.row_index))
    )
    ok_items = [item for item in items if item.ok]

    confidences = [item.confidence for item in ok_items if item.confidence is not None]
    labelled = [item for item in ok_items if item.label and item.predicted]
    correct = [
        item.label.strip().lower() == item.predicted.strip().lower() for item in labelled
    ]
    labelled_confidences = [item.confidence for item in labelled if item.confidence is not None]

    distribution: dict[str, int] = {}
    for item in ok_items:
        key = item.predicted or "(none)"
        distribution[key] = distribution.get(key, 0) + 1

    summary: dict[str, Any] = {
        "total": len(items),
        "processed": len(ok_items),
        "failed": len(items) - len(ok_items),
        "labelled": len(labelled),
        "mean_confidence": (
            round(sum(confidences) / len(confidences), 6) if confidences else None
        ),
        "prediction_distribution": distribution,
        "errors": [item.error for item in items if not item.ok][:10],
        # Present-but-null rather than absent, so clients can read the keys
        # unconditionally and the shape does not depend on whether labels existed.
        "accuracy": None,
        "ece": None,
        "mce": None,
        "brier": None,
        "calibration_note": "No labelled rows in this batch; upload a label column to get accuracy and ECE.",
        "band_counts": None,
    }

    if labelled:
        # Accuracy depends on the labels alone: a row is right or wrong whether or not
        # its confidence was recorded. ECE, Brier and the routing bands do need one
        # confidence per row, so those stay behind the completeness check below.
        summary["accuracy"] = sum(1 for outcome in correct if outcome) / len(correct)
        if len(labelled_confidences) == len(labelled):
            report = calibration_report(labelled_confidences, correct)
            summary.update(
                {
                    "accuracy": report.accuracy,
                    "ece": report.ece,
                    "mce": report.mce,
                    "brier": report.brier,
                    "calibration_note": report.note,
                    "reliability_bins": [item.to_dict() for item in report.bins],
                }
            )
            # How often would the hybrid ladder have needed help?
            summary["band_counts"] = _band_counts(labelled_confidences)
        else:
            summary["calibration_note"] = (
                f"{len(labelled)} labelled row(s), but "
                f"{len(labelled) - len(labelled_confidences)} of them recorded no "
                "confidence, so ECE, Brier and the routing bands are withheld for this "
                "batch. Accuracy is unaffected."
            )

    return summary


def _band_counts(confidences: list[float]) -> dict[str, int]:
    from ..config import get_settings

    settings = get_settings()
    counts = {"auto": 0, "verify": 0, "review": 0}
    for value in confidences:
        if value >= settings.auto_threshold:
            counts["auto"] += 1
        elif value >= settings.verify_threshold:
            counts["verify"] += 1
        else:
            counts["review"] += 1
    return counts


def state_preview(job: BulkJob, limit: int = 5) -> list[dict[str, Any]]:
    """First few rows of a job, for the upload confirmation screen."""
    items = job.items[:limit] if getattr(job, "items", None) else []
    return [
        {"row_index": item.row_index, "state": item.state, "label": item.label} for item in items
    ]


def row_text(row: dict[str, Any]) -> str:
    return flatten_state(row)
