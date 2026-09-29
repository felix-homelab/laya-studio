"""SQLAlchemy models for Laya Studio.

The schema is organised around one central fact: **an evaluation is a
first-class, immutable record**. Everything else (bulk jobs, A/B tests, workflow
runs, review items, calibration snapshots) points back at an evaluation, so the
analytics dashboard and the calibration monitor can be computed from a single
audit trail.

**These models are a query layer, not the schema authority.** PostgreSQL DDL is
owned by ``prisma/schema.prisma`` and applied with ``prisma migrate``. This module
must stay in step with it; ``tests/test_schema_drift.py`` migrates a fresh database
with Prisma and fails if the two disagree.

Two type choices follow from that, both matching Prisma's PostgreSQL defaults:

* ``Text`` rather than ``String(n)``. PostgreSQL treats ``varchar(n)`` and ``text``
  identically for performance, Prisma maps ``String`` to ``text``, and the length
  limits we actually enforce are enforced in the application.
* ``JSONB`` rather than ``JSON``. Prisma maps ``Json`` to ``jsonb``, which is also
  the indexable, deduplicating choice on PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# --- Enum-ish string vocabularies (kept as plain strings for portability) ----

PRIMITIVES = ("choice", "score", "noul")
PRIMITIVE_LABELS = {
    "choice": "Choice",
    "score": "Score",
    "noul": "Noul",
}

EVALUATION_SOURCES = (
    "playground",
    "api",
    "webhook",
    "email",
    "bulk",
    "workflow",
    "ab_test",
)

ROUTES = ("auto", "verify", "review", "error")
RUNTIME_STATUS = ("pending", "running", "succeeded", "failed", "cancelled")
REVIEW_STATUS = ("pending", "in_review", "approved", "corrected", "rejected")
ACTION_KINDS = ("webhook", "email", "llm_verify", "review", "noop")
ACTION_STATUS = ("ok", "failed", "skipped")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# =============================================================================
# Part 3 — Custom schema & template management
# =============================================================================


class EvalSchema(Base, TimestampMixin):
    """A reusable, named bundle of Laya typed questions."""

    __tablename__ = "eval_schemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # "choice" | "score" | "noul" when the schema is single-primitive, else "mixed".
    primitive: Mapped[str] = mapped_column(Text, default="choice", nullable=False)
    # The exact dict handed to ``Router.predict(state, questions)``.
    definition: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    tags: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    revisions: Mapped[list[SchemaRevision]] = relationship(
        back_populates="schema",
        cascade="all, delete-orphan",
        order_by="SchemaRevision.revision.desc()",
    )
    evaluations: Mapped[list[Evaluation]] = relationship(back_populates="schema")

    @property
    def questions(self) -> dict:
        return dict(self.definition.get("questions", {}))

    @property
    def question_names(self) -> list[str]:
        return list(self.definition.get("questions", {}).keys())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EvalSchema {self.key!r} rev={self.current_revision}>"


class SchemaRevision(Base):
    """Immutable snapshot of a schema definition.

    Revisions exist so A/B tests and historical evaluations can always be
    replayed against the taxonomy that was live at the time.
    """

    __tablename__ = "schema_revisions"
    __table_args__ = (UniqueConstraint("schema_id", "revision", name="uq_schema_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # No index of its own: ``uq_schema_revision`` leads with this column.
    schema_id: Mapped[int] = mapped_column(
        ForeignKey("eval_schemas.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    schema: Mapped[EvalSchema] = relationship(back_populates="revisions")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SchemaRevision schema={self.schema_id} rev={self.revision}>"


# =============================================================================
# Part 1 — Interactive decision & query playground
# =============================================================================


class Evaluation(Base, TimestampMixin):
    """One forward pass of Laya over one state, with everything it returned."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schema_id: Mapped[int | None] = mapped_column(
        ForeignKey("eval_schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    schema_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("schema_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Overrides the mixin's column only to drop its index: the leading column of
    # ``ix_evaluations_created_source`` already serves created_at lookups.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    # The state handed to the model (dict or {"text": ...}).
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Flattened, human-readable rendering — used for search and review cards.
    state_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # The question set actually sent, captured for replay.
    questions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Raw ``result["answers"]`` exactly as the engine returned it.
    answers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Normalised per-question view consumed by the visualizer.
    question_results: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # ``result["routing"]`` — which checkpoint answered and why.
    routing: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # Which engine produced this row. Always "laya" today; recorded because it
    # is the first thing you want to know about a historical decision.
    engine: Mapped[str] = mapped_column(Text, default="laya", nullable=False)
    # Which checkpoint answered: english | multilingual | typed-decisions.
    engine_model: Mapped[str] = mapped_column(Text, default="unknown", nullable=False)

    # Denormalised "headline" of the run, for fast dashboards.
    primary_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    top_choice: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True, index=True)
    raw_confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    calibrated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    latency_ms: Mapped[float] = mapped_column(Double, default=0.0, nullable=False)
    source: Mapped[str] = mapped_column(Text, default="playground", nullable=False, index=True)
    # Ground truth, when a human or an external label tells us the answer.
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    schema: Mapped[EvalSchema | None] = relationship(back_populates="evaluations")

    @property
    def is_correct(self) -> bool | None:
        if self.label is None or self.top_choice is None:
            return None
        return self.label.strip().lower() == self.top_choice.strip().lower()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Evaluation {self.id} {self.top_choice}@{self.confidence}>"


class BulkJob(Base, TimestampMixin):
    """A CSV/JSON upload evaluated row-by-row against one schema."""

    __tablename__ = "bulk_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    schema_id: Mapped[int | None] = mapped_column(
        ForeignKey("eval_schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    filename: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False, index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # {"state_column": ..., "label_column": ..., "model": ...}
    options: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # {"accuracy": float, "mean_confidence": float, "ece": float, ...}
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[BulkJobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="BulkJobItem.row_index"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<BulkJob {self.id} {self.status} {self.processed_rows}/{self.total_rows}>"


class BulkJobItem(Base):
    __tablename__ = "bulk_job_items"
    # One item per input row. Also the job_id index: it leads with job_id.
    __table_args__ = (UniqueConstraint("job_id", "row_index", name="uq_bulk_job_item_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("bulk_jobs.id", ondelete="CASCADE"), nullable=False
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    predicted: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    job: Mapped[BulkJob] = relationship(back_populates="items")


class ABTest(Base, TimestampMixin):
    """Two schema revisions evaluated over one shared dataset."""

    __tablename__ = "ab_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    schema_id: Mapped[int | None] = mapped_column(
        ForeignKey("eval_schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision_a_id: Mapped[int | None] = mapped_column(
        ForeignKey("schema_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    revision_b_id: Mapped[int | None] = mapped_column(
        ForeignKey("schema_revisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False, index=True)
    dataset: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # {"flip_rate": ..., "mean_confidence_delta": ..., "agreement": ...}
    summary: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list[ABTestItem]] = relationship(
        back_populates="test", cascade="all, delete-orphan", order_by="ABTestItem.row_index"
    )


class ABTestItem(Base):
    __tablename__ = "ab_test_items"
    # One item per dataset row. Also the test_id index: it leads with test_id.
    __table_args__ = (UniqueConstraint("test_id", "row_index", name="uq_ab_test_item_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    test_id: Mapped[int] = mapped_column(
        ForeignKey("ab_tests.id", ondelete="CASCADE"), nullable=False
    )
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    choice_a: Mapped[str | None] = mapped_column(Text, nullable=True)
    choice_b: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_a: Mapped[float | None] = mapped_column(Double, nullable=True)
    confidence_b: Mapped[float | None] = mapped_column(Double, nullable=True)
    flipped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result_a: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result_b: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    test: Mapped[ABTest] = relationship(back_populates="items")


# =============================================================================
# Part 2 — Workflow automation engine
# =============================================================================


class Workflow(Base, TimestampMixin):
    """A trigger + a schema + a confidence ladder + downstream actions."""

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    # api | webhook | email | manual
    trigger_type: Mapped[str] = mapped_column(Text, default="api", nullable=False)
    trigger_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    schema_id: Mapped[int | None] = mapped_column(
        ForeignKey("eval_schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which question drives the routing decision. Empty = first question.
    decision_question: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ordered steps, e.g. [{"on": "auto", "kind": "webhook", "config": {...}}]
    steps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    auto_threshold: Mapped[float] = mapped_column(Double, default=0.90, nullable=False)
    verify_threshold: Mapped[float] = mapped_column(Double, default=0.60, nullable=False)

    runs: Mapped[list[WorkflowRun]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowRun.created_at.desc()",
    )
    schema: Mapped[EvalSchema | None] = relationship()

    @property
    def route_ladder(self) -> list[tuple[str, float]]:
        return [
            ("auto", self.auto_threshold),
            ("verify", self.verify_threshold),
        ]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Workflow {self.key!r} enabled={self.enabled}>"


class WorkflowRun(Base, TimestampMixin):
    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False, index=True)
    # Indexed through ``ix_workflow_runs_route_created``, which leads with it.
    route: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    # Resolved verdict after any verification step.
    final_choice: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ordered [{kind, status, detail}]
    actions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    trigger_payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workflow: Mapped[Workflow] = relationship(back_populates="runs")
    action_logs: Mapped[list[ActionLog]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ActionLog.id"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<WorkflowRun {self.id} {self.status} route={self.route}>"


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    target: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(Text, default="ok", nullable=False, index=True)
    request: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Double, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    run: Mapped[WorkflowRun | None] = relationship(back_populates="action_logs")


# =============================================================================
# Part 4 — Real-time analytics, calibration & manual review
# =============================================================================


class ReviewItem(Base, TimestampMixin):
    """A low-confidence (or flagged) item parked for a human."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    workflow_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), nullable=True, index=True
    )

    state: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    state_text: Mapped[str] = mapped_column(Text, default="", nullable=False)

    question: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_choice: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    alternatives: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    reason: Mapped[str] = mapped_column(Text, default="low_confidence", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    # Indexed through ``ix_review_items_status_priority``, which leads with it.
    status: Mapped[str] = mapped_column(Text, default="pending", nullable=False)
    assignee: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ReviewItem {self.id} {self.status} {self.model_choice}>"


class CalibrationSnapshot(Base):
    """A fitted temperature + reliability summary for one question surface."""

    __tablename__ = "calibration_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    primitive: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Which question this fit belongs to; empty string = schema-wide.
    question: Mapped[str] = mapped_column(Text, default="", nullable=False, index=True)
    schema_id: Mapped[int | None] = mapped_column(
        ForeignKey("eval_schemas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    n_samples: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # The fitted map is `sigmoid(slope * logit(p) + intercept)`. One temperature is
    # not enough: it cannot move a confidence across 0.5, which is exactly the
    # correction Laya's entropy-based confidence needs. See ADR 0010.
    slope: Mapped[float] = mapped_column(Double, default=1.0, nullable=False)
    intercept: Mapped[float] = mapped_column(Double, default=0.0, nullable=False)
    # `1 / slope`, stored so the UI can show a familiar single number. Null when the
    # slope is zero (a base-rate map), where no temperature exists.
    temperature: Mapped[float | None] = mapped_column(Double, nullable=True)
    ece: Mapped[float | None] = mapped_column(Double, nullable=True)
    mce: Mapped[float | None] = mapped_column(Double, nullable=True)
    brier: Mapped[float | None] = mapped_column(Double, nullable=True)
    accuracy: Mapped[float | None] = mapped_column(Double, nullable=True)
    mean_confidence: Mapped[float | None] = mapped_column(Double, nullable=True)
    bins: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CalibrationSnapshot {self.primitive}/{self.question} T={self.temperature}>"


Index("ix_evaluations_created_source", Evaluation.created_at, Evaluation.source)
Index("ix_workflow_runs_route_created", WorkflowRun.route, WorkflowRun.created_at)
Index("ix_review_items_status_priority", ReviewItem.status, ReviewItem.priority)
