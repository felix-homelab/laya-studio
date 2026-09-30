"""Bundled sample datasets.

These exist so a fresh deployment has something real to measure. They are plain
CSV files of test inputs with ground-truth labels — the same thing you would
upload on the Bulk page — evaluated by the **real** engine through the normal
bulk path. Nothing here fabricates an evaluation.

The 40-row email-triage set is deliberately mixed: mostly clear cases, plus a few
genuinely ambiguous ones and three non-English rows, so the confidence spread and
the review queue have something to show.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from .models import BulkJob, EvalSchema
from .services import bulk as bulk_service
from .services import decision_schemas

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "datasets"

#: ``filename -> (schema key, human label)``
SAMPLE_DATASETS: dict[str, tuple[str, str]] = {
    "email-triage-sample.csv": (
        "email-triage",
        "Email triage sample (40 labelled rows)",
    ),
}


class SampleError(ValueError):
    """Raised when a sample cannot be loaded."""


def available_samples() -> list[dict[str, str]]:
    """The bundled datasets that are actually present on disk."""
    found: list[dict[str, str]] = []
    for filename, (schema_key, description) in SAMPLE_DATASETS.items():
        path = SAMPLES_DIR / filename
        if path.is_file():
            found.append(
                {"filename": filename, "schema": schema_key, "description": description}
            )
    return found


def load_sample(
    db: Session,
    filename: str = "email-triage-sample.csv",
    *,
    state_column: str | None = None,
    label_column: str = "truth",
    model: str | None = None,
) -> tuple[EvalSchema, BulkJob]:
    """Evaluate a bundled dataset end to end and return its bulk job.

    Goes through ``bulk.create_job`` / ``attach_rows`` / ``run_job`` — the same
    code the upload endpoint uses — so the result is an ordinary job you can
    inspect, rerun and A/B against.

    Raises :class:`SampleError` for anything wrong with the sample itself, bulk
    validation included. ``EngineNotReady`` / ``EngineUnavailable`` from the run
    propagate unchanged: they are about the engine, not the sample.
    """
    if filename not in SAMPLE_DATASETS:
        raise SampleError(
            f"Unknown sample {filename!r}. Available: {', '.join(SAMPLE_DATASETS) or 'none'}."
        )

    path = SAMPLES_DIR / filename
    if not path.is_file():
        raise SampleError(f"Sample dataset not found on disk: {path}")

    schema_key = SAMPLE_DATASETS[filename][0]
    decision_schemas.seed_builtin_templates(db)
    schema = decision_schemas.get_schema_by_key(db, schema_key)
    if schema is None:
        raise SampleError(f"Sample {filename!r} needs schema {schema_key!r}, which is missing.")

    content = path.read_bytes()
    try:
        dataset = bulk_service.parse_dataset(content, filename)

        job = bulk_service.create_job(
            db,
            name=SAMPLE_DATASETS[filename][1],
            dataset=dataset,
            filename=filename,
            schema=schema,
            state_column=state_column,
            label_column=label_column,
            model=model,
        )
        bulk_service.attach_rows(db, job, dataset)
        bulk_service.run_job(db, job)
    except bulk_service.BulkError as exc:
        raise SampleError(f"Sample {filename!r} could not be evaluated: {exc}") from exc
    return schema, job
