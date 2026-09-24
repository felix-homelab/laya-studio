"""Part 3 — custom schema & template management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..schemas import (
    LintFinding,
    LintResponse,
    RevisionSummary,
    SchemaCreate,
    SchemaDetail,
    SchemaSummary,
    SchemaUpdate,
)
from ..services import decision_schemas

router = APIRouter(prefix="/api/schemas", tags=["schemas"])


@router.get("", response_model=list[SchemaSummary])
def list_schemas(
    include_templates: bool = True,
    search: str | None = None,
    tag: str | None = None,
    db: Session = Depends(get_db),
) -> list[SchemaSummary]:
    rows = decision_schemas.list_schemas(
        db, include_templates=include_templates, search=search, tag=tag
    )
    return [SchemaSummary.model_validate(row) for row in rows]


@router.post("", response_model=SchemaDetail, status_code=status.HTTP_201_CREATED)
def create_schema(payload: SchemaCreate, db: Session = Depends(get_db)) -> SchemaDetail:
    questions = {
        name: definition.model_dump(exclude_none=True)
        for name, definition in payload.questions.items()
    }
    try:
        schema = decision_schemas.create_schema(
            db,
            name=payload.name,
            description=payload.description,
            questions=questions,
            tags=payload.tags,
            key=payload.key,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return SchemaDetail.model_validate(schema)


@router.get("/{schema_id}", response_model=SchemaDetail)
def get_schema(schema_id: int, db: Session = Depends(get_db)) -> SchemaDetail:
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")
    return SchemaDetail.model_validate(schema)


@router.put("/{schema_id}", response_model=SchemaDetail)
def update_schema(
    schema_id: int,
    payload: SchemaUpdate,
    db: Session = Depends(get_db),
) -> SchemaDetail:
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")

    questions = (
        {
            name: definition.model_dump(exclude_none=True)
            for name, definition in payload.questions.items()
        }
        if payload.questions is not None
        else None
    )

    try:
        schema = decision_schemas.update_schema(
            db,
            schema,
            name=payload.name,
            description=payload.description,
            questions=questions,
            tags=payload.tags,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return SchemaDetail.model_validate(schema)


@router.delete("/{schema_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schema(schema_id: int, db: Session = Depends(get_db)) -> None:
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")
    if schema.is_template:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Built-in templates cannot be deleted. Fork it into an editable copy instead.",
        )
    decision_schemas.delete_schema(db, schema)


@router.post("/{schema_id}/fork", response_model=SchemaDetail, status_code=status.HTTP_201_CREATED)
def fork_schema(
    schema_id: int,
    name: str = Query(min_length=1, max_length=200),
    db: Session = Depends(get_db),
) -> SchemaDetail:
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")
    forked = decision_schemas.fork_schema(db, schema, name=name)
    return SchemaDetail.model_validate(forked)


@router.get("/{schema_id}/revisions", response_model=list[RevisionSummary])
def list_revisions(schema_id: int, db: Session = Depends(get_db)) -> list[RevisionSummary]:
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")
    return [RevisionSummary.model_validate(row) for row in decision_schemas.list_revisions(db, schema)]


@router.get("/{schema_id}/lint", response_model=LintResponse)
def lint_schema(schema_id: int, db: Session = Depends(get_db)) -> LintResponse:
    schema = decision_schemas.get_schema(db, schema_id)
    if schema is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No schema {schema_id}.")
    findings = decision_schemas.lint(schema.questions)
    return LintResponse(
        findings=[LintFinding(**item) for item in findings],
        ok=decision_schemas.lint_ok(schema.questions),
    )
