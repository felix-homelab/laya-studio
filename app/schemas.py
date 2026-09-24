"""Pydantic request/response models for the JSON API.

These are the wire contract. The ORM models in :mod:`app.models` are deliberately
richer (they carry denormalised analytics columns); the response models here
expose the subset the UI and API clients actually consume.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Primitive = Literal["choice", "score", "noul"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# Questions & schemas
# =============================================================================


class QuestionDefinition(BaseModel):
    """One Laya typed question, as stored in a schema definition."""

    type: Primitive
    instructions: str = ""
    #: dict of label -> description for ``choice``; ordered list of levels for
    #: ``score``; optional for ``noul``.
    criteria: Any = None


class LintFinding(BaseModel):
    severity: Literal["error", "warning"]
    question: str = ""
    message: str


class LintRequest(BaseModel):
    """Deliberately permissive.

    A linter that rejects malformed input at the schema-validation layer cannot
    tell you *what* is malformed. These questions are intentionally untyped so
    :func:`app.inference.base.validate_questions` can report on them.
    """

    questions: dict[str, Any]

    @field_validator("questions")
    @classmethod
    def _non_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("Provide at least one question.")
        return value


class LintResponse(BaseModel):
    findings: list[LintFinding]
    ok: bool


class SchemaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    questions: dict[str, QuestionDefinition]
    tags: list[str] = Field(default_factory=list)
    key: str | None = None


class SchemaUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    questions: dict[str, QuestionDefinition] | None = None
    tags: list[str] | None = None
    note: str = ""


class SchemaSummary(ORMModel):
    id: int
    key: str
    name: str
    description: str
    primitive: str
    tags: list[str] = Field(default_factory=list)
    is_template: bool
    current_revision: int
    created_at: datetime
    updated_at: datetime


class SchemaDetail(SchemaSummary):
    definition: dict[str, Any]


class RevisionSummary(ORMModel):
    id: int
    revision: int
    note: str
    created_at: datetime
    definition: dict[str, Any]


# =============================================================================
# Playground
# =============================================================================


class EvaluationRequest(BaseModel):
    #: A free-text state, or a structured dict (email, ticket, arbitrary JSON).
    state: dict[str, Any] | str
    schema_id: int | None = None
    schema_key: str | None = None
    #: Inline override; wins over the schema when present.
    questions: dict[str, QuestionDefinition] | None = None
    model: str | None = Field(default=None, description="english | multilingual | typed-decisions")
    primary_question: str | None = None
    apply_calibration: bool = True
    persist: bool = True


class BreakdownEntry(BaseModel):
    label: str
    probability: float
    rank: int
    is_top: bool


class QuestionResult(BaseModel):
    question: str
    type: str
    instructions: str = ""
    choice: str | None = None
    level: str | None = None
    confidence: float | None = None
    #: Post-calibration confidence. `confidence` is the value the model reported
    #: (Laya's entropy-based measure); this is what the ladder and the dashboards use.
    raw_confidence: float | None = None
    calibrated: bool = False
    #: `1 / slope` of the fitted map, for display. None when no map was applied.
    temperature: float | None = None
    calibration_slope: float | None = None
    calibration_intercept: float | None = None
    noul: float | None = None
    score: float | None = None
    max_score: float | None = None
    #: Laya's own trained act/escalate head, when the checkpoint emits one.
    act_probability: float | None = None
    rubric: list[str] = Field(default_factory=list)
    breakdown: list[BreakdownEntry] = Field(default_factory=list)
    alternatives: list[BreakdownEntry] = Field(default_factory=list)
    band: str = "unknown"


class EvaluationResponse(BaseModel):
    id: int | None = None
    schema_id: int | None = None
    state: dict[str, Any]
    state_text: str = ""
    question_results: list[QuestionResult] = Field(default_factory=list)
    routing: dict[str, Any] = Field(default_factory=dict)
    engine: str
    engine_model: str
    primary_question: str | None = None
    top_choice: str | None = None
    confidence: float | None = None
    raw_confidence: float | None = None
    calibrated: bool = False
    latency_ms: float = 0.0
    source: str = "playground"
    label: str | None = None
    created_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    calibration: dict[str, Any] = Field(default_factory=dict)


class RouteRequest(BaseModel):
    state: dict[str, Any] | str


class RouteResponse(BaseModel):
    routing: dict[str, Any]


class LabelRequest(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    label_source: str = "human"


# =============================================================================
# Bulk
# =============================================================================


class BulkJobSummary(ORMModel):
    id: int
    name: str
    schema_id: int | None
    filename: str
    status: str
    total_rows: int
    processed_rows: int
    failed_rows: int
    options: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class BulkItemResponse(ORMModel):
    id: int
    row_index: int
    state: dict[str, Any]
    label: str | None = None
    predicted: str | None = None
    confidence: float | None = None
    ok: bool
    error: str | None = None
    evaluation_id: int | None = None


class BulkJobDetail(BulkJobSummary):
    items: list[BulkItemResponse] = Field(default_factory=list)


# =============================================================================
# A/B testing
# =============================================================================


class ABTestCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    schema_id: int
    revision_a_id: int
    revision_b_id: int
    #: Either an explicit dataset, or a bulk job whose rows to reuse.
    dataset: list[dict[str, Any]] | None = None
    source_job_id: int | None = None
    model: str | None = None

    @field_validator("dataset")
    @classmethod
    def _dataset_shape(cls, value: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if value is None:
            return None
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                raise ValueError(f"Row {index} must be an object with a 'state' key.")
        return value


class ABTestSummary(ORMModel):
    id: int
    name: str
    schema_id: int | None
    revision_a_id: int | None
    revision_b_id: int | None
    status: str
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class ABTestItemResponse(ORMModel):
    id: int
    row_index: int
    state: dict[str, Any]
    label: str | None = None
    choice_a: str | None = None
    choice_b: str | None = None
    confidence_a: float | None = None
    confidence_b: float | None = None
    flipped: bool
    result_a: dict[str, Any] = Field(default_factory=dict)
    result_b: dict[str, Any] = Field(default_factory=dict)


class ABTestDetail(ABTestSummary):
    items: list[ABTestItemResponse] = Field(default_factory=list)


# =============================================================================
# Workflows
# =============================================================================


class WorkflowStep(BaseModel):
    on: Literal["auto", "verify", "review", "always"] = "auto"
    kind: Literal["webhook", "email", "review", "noop"] = "noop"
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    schema_id: int | None = None
    trigger_type: Literal["api", "webhook", "email", "manual"] = "api"
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(default_factory=list)
    decision_question: str | None = None
    auto_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    verify_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    enabled: bool = True
    key: str | None = None


class WorkflowUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    enabled: bool | None = None
    trigger_type: Literal["api", "webhook", "email", "manual"] | None = None
    trigger_config: dict[str, Any] | None = None
    steps: list[WorkflowStep] | None = None
    schema_id: int | None = None
    decision_question: str | None = None
    auto_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    verify_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class WorkflowSummary(ORMModel):
    id: int
    key: str
    name: str
    description: str
    enabled: bool
    trigger_type: str
    trigger_config: dict[str, Any] = Field(default_factory=dict)
    schema_id: int | None
    decision_question: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    auto_threshold: float
    verify_threshold: float
    created_at: datetime
    updated_at: datetime


class ActionLogResponse(ORMModel):
    id: int
    kind: str
    target: str
    status: str
    request: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    latency_ms: float
    created_at: datetime


class WorkflowRunSummary(ORMModel):
    id: int
    workflow_id: int
    evaluation_id: int | None
    status: str
    route: str | None
    confidence: float | None
    final_choice: str | None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    trigger_payload: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WorkflowRunDetail(WorkflowRunSummary):
    action_logs: list[ActionLogResponse] = Field(default_factory=list)
    evaluation: EvaluationResponse | None = None


class WorkflowRunResponse(BaseModel):
    run: WorkflowRunSummary
    route: str
    effective_route: str
    verification: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: EvaluationResponse | None = None
    error: str | None = None


# =============================================================================
# Ingestion
# =============================================================================


class IngestRequest(BaseModel):
    """Payload accepted by the multichannel triggers."""

    state: dict[str, Any] | str | None = None
    #: Convenience for plain-text senders.
    text: str | None = None
    #: Free-form metadata persisted on the run for auditing.
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Skip the confidence ladder and just evaluate.
    dry_run: bool = False

    def resolve_state(self) -> dict[str, Any] | str:
        if self.state is not None:
            return self.state
        if self.text is not None:
            return {"text": self.text}
        raise ValueError("Provide either `state` or `text`.")


class EmailIngestRequest(BaseModel):
    """Shape of a parsed inbound email."""

    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    subject: str | None = None
    body: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


# =============================================================================
# Analytics
# =============================================================================


class CalibrationRefitResponse(BaseModel):
    snapshots: list[dict[str, Any]] = Field(default_factory=list)
    message: str


# =============================================================================
# Review queue
# =============================================================================


class ReviewItemResponse(ORMModel):
    id: int
    workflow_id: int | None
    workflow_run_id: int | None
    evaluation_id: int | None
    state: dict[str, Any]
    state_text: str
    question: str | None
    model_choice: str | None
    model_confidence: float | None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    reason: str
    priority: int
    status: str
    assignee: str | None
    corrected_label: str | None
    notes: str
    created_at: datetime
    resolved_at: datetime | None = None


class ReviewResolveRequest(BaseModel):
    status: Literal["approved", "corrected", "rejected", "in_review"]
    corrected_label: str | None = None
    notes: str = ""
    assignee: str | None = None


class ReviewClaimRequest(BaseModel):
    assignee: str = Field(min_length=1, max_length=120)


# =============================================================================
# Health
# =============================================================================
