"""Schema CRUD, revisioning and the built-in template library.

A "schema" is a named bundle of Laya typed questions. Revisions are immutable:
editing a schema mints a new :class:`~app.models.SchemaRevision` rather than
mutating history, so an A/B test or a three-month-old evaluation can always be
replayed against the exact taxonomy that produced it.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..inference.base import has_errors, validate_questions
from ..models import PRIMITIVES, EvalSchema, SchemaRevision

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = SLUG_RE.sub("-", ascii_only).strip("-")
    return slug or "schema"


def unique_key(db: Session, base: str) -> str:
    """Return a collision-free schema key derived from ``base``."""
    candidate = slugify(base)
    suffix = 2
    while db.scalar(select(EvalSchema.id).where(EvalSchema.key == candidate)) is not None:
        candidate = f"{slugify(base)}-{suffix}"
        suffix += 1
    return candidate


def derive_primitive(questions: dict[str, dict[str, Any]]) -> str:
    """``choice``/``score``/``noul`` for single-primitive schemas, else ``mixed``."""
    kinds = {
        str(definition.get("type"))
        for definition in questions.values()
        if isinstance(definition, dict)
    }
    kinds &= set(PRIMITIVES)
    if len(kinds) == 1:
        return next(iter(kinds))
    if not kinds:
        return "choice"
    return "mixed"


def normalize_definition(questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Canonical stored form of a question bundle.

    Deep-copied, so nested ``criteria`` are never aliased to the caller's input
    (or to ``TEMPLATE_DEFINITIONS``) and immutable revisions stay immutable.
    """
    return {"questions": {str(name): copy.deepcopy(dict(definition)) for name, definition in questions.items()}}


def lint(questions: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Lint a question bundle for the no-code editor."""
    return validate_questions(questions)


def lint_ok(questions: dict[str, dict[str, Any]]) -> bool:
    return not has_errors(lint(questions))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def list_schemas(
    db: Session,
    *,
    include_templates: bool = True,
    search: str | None = None,
    tag: str | None = None,
    limit: int = 200,
) -> list[EvalSchema]:
    statement = select(EvalSchema)
    if not include_templates:
        statement = statement.where(EvalSchema.is_template.is_(False))
    if search:
        pattern = f"%{search.strip().lower()}%"
        statement = statement.where(
            EvalSchema.name.ilike(pattern) | EvalSchema.key.ilike(pattern)
        )
    if tag:
        # Filter before LIMIT (JSONB `@>`), or matches past the first page are lost.
        statement = statement.where(EvalSchema.tags.contains([tag]))
    statement = statement.order_by(EvalSchema.is_template.asc(), EvalSchema.name.asc())
    return list(db.scalars(statement.limit(limit)))


def get_schema(db: Session, schema_id: int) -> EvalSchema | None:
    return db.get(EvalSchema, schema_id)


def get_schema_by_key(db: Session, key: str) -> EvalSchema | None:
    return db.scalar(select(EvalSchema).where(EvalSchema.key == key))


def resolve_schema(db: Session, identifier: int | str) -> EvalSchema | None:
    """Look a schema up by primary key (int) or key/name (str)."""
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        return get_schema(db, int(identifier))
    return get_schema_by_key(db, slugify(str(identifier))) or get_schema_by_key(db, str(identifier))


def get_revision(db: Session, revision_id: int) -> SchemaRevision | None:
    return db.get(SchemaRevision, revision_id)


def list_revisions(db: Session, schema: EvalSchema) -> list[SchemaRevision]:
    return list(
        db.scalars(
            select(SchemaRevision)
            .where(SchemaRevision.schema_id == schema.id)
            .order_by(SchemaRevision.revision.desc())
        )
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def create_schema(
    db: Session,
    *,
    name: str,
    questions: dict[str, dict[str, Any]],
    description: str = "",
    tags: Iterable[str] | None = None,
    is_template: bool = False,
    key: str | None = None,
    note: str = "Initial revision",
    commit: bool = True,
) -> EvalSchema:
    """Create a schema plus its first revision."""
    findings = lint(questions)
    if has_errors(findings):
        raise ValueError(_format_lint_error(findings))

    definition = normalize_definition(questions)
    schema = EvalSchema(
        key=key or unique_key(db, name),
        name=name.strip() or "Untitled schema",
        description=description.strip(),
        primitive=derive_primitive(definition["questions"]),
        definition=definition,
        tags=list(tags or []),
        is_template=is_template,
        current_revision=1,
    )
    db.add(schema)
    db.flush()

    db.add(
        SchemaRevision(
            schema_id=schema.id,
            revision=1,
            definition=definition,
            note=note,
        )
    )
    if commit:
        db.commit()
        db.refresh(schema)
    return schema


def update_schema(
    db: Session,
    schema: EvalSchema,
    *,
    name: str | None = None,
    description: str | None = None,
    questions: dict[str, dict[str, Any]] | None = None,
    tags: Iterable[str] | None = None,
    note: str = "",
    commit: bool = True,
) -> EvalSchema:
    """Update metadata freely; mint a new revision when questions change."""
    if name is not None:
        schema.name = name.strip() or schema.name
    if description is not None:
        schema.description = description.strip()
    if tags is not None:
        schema.tags = list(tags)

    if questions is not None:
        findings = lint(questions)
        if has_errors(findings):
            raise ValueError(_format_lint_error(findings))

        definition = normalize_definition(questions)
        # Lock the schema row and read the committed revision, so concurrent edits
        # mint consecutive revisions instead of colliding on uq_schema_revision.
        # Flush first so an unflushed earlier edit in this session is what we read.
        db.flush()
        locked = db.execute(
            select(EvalSchema.current_revision, EvalSchema.definition)
            .where(EvalSchema.id == schema.id)
            .with_for_update()
        ).first()
        current_revision, current_definition = (
            locked if locked is not None else (schema.current_revision, schema.definition)
        )
        if definition != current_definition:
            schema.current_revision = current_revision + 1
            schema.definition = definition
            schema.primitive = derive_primitive(definition["questions"])
            db.add(
                SchemaRevision(
                    schema_id=schema.id,
                    revision=schema.current_revision,
                    definition=definition,
                    note=note or f"Revision {schema.current_revision}",
                )
            )

    if commit:
        db.commit()
        db.refresh(schema)
    return schema


def fork_schema(
    db: Session,
    schema: EvalSchema,
    *,
    name: str,
    commit: bool = True,
) -> EvalSchema:
    """Copy a schema (typically a template) into an editable, non-template copy."""
    return create_schema(
        db,
        name=name,
        questions=schema.questions,
        description=schema.description,
        tags=list(schema.tags or []),
        is_template=False,
        note=f"Forked from {schema.key} r{schema.current_revision}",
        commit=commit,
    )


def delete_schema(db: Session, schema: EvalSchema, *, commit: bool = True) -> None:
    db.delete(schema)
    if commit:
        db.commit()


def _format_lint_error(findings: list[dict[str, str]]) -> str:
    errors = [item for item in findings if item["severity"] == "error"]
    parts = [f"{item['question'] or '(schema)'}: {item['message']}" for item in errors]
    return "; ".join(parts) or "Invalid schema definition."


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------

TEMPLATE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "key": "email-triage",
        "name": "Email triage",
        "description": (
            "Route an inbound email to a department, score urgency, and flag churn "
            "and refund intent. Demonstrates all three primitives in one pass."
        ),
        "tags": ["support", "triage", "starter"],
        "questions": {
            "department": {
                "type": "choice",
                "instructions": "Which department should handle this request?",
                "criteria": {
                    "billing": "invoices, payments, refunds, duplicate charges",
                    "technical": "bugs, outages, system errors, integration failures",
                    "sales": "pricing, new contracts, upgrades, renewals",
                    "other": "everything else, including general questions",
                },
            },
            "urgency": {
                "type": "score",
                "instructions": "How urgent is this request?",
                "criteria": [
                    "informational, no deadline, nothing blocked",
                    "wants a response soon, workaround available",
                    "blocking outage, deadline missed, threatening to leave",
                ],
            },
            "churn_risk": {
                "type": "noul",
                "instructions": "Does the sender threaten to cancel, leave, or churn?",
                "criteria": {
                    "yes": "threatens to cancel, leave, switch vendors, or not renew",
                    "no": "reports a problem and gives no indication of walking away",
                },
            },
            "refund_requested": {
                "type": "noul",
                "instructions": "Does the sender explicitly request a refund or a charge reversal?",
                "criteria": {
                    "yes": "asks for money back, or for a duplicated charge to be reversed",
                    "no": "describes the situation and requests that nothing change",
                },
            },
        },
    },
    {
        "key": "fraud-detection",
        "name": "Fraud detection",
        "description": (
            "Score a transaction or account narrative for fraud likelihood and "
            "identify the dominant fraud pattern for investigator routing."
        ),
        "tags": ["risk", "fraud", "guardrail"],
        "questions": {
            "fraud_type": {
                "type": "choice",
                "instructions": "What kind of fraud does this narrative most resemble?",
                "criteria": {
                    "account_takeover": "credentials stolen, unfamiliar device or location",
                    "payment_fraud": "stolen card, chargeback, unauthorised purchase",
                    "identity_theft": "impersonation, forged documents, synthetic identity",
                    "friendly_fraud": "dispute from a customer who received the goods",
                    "no_fraud": "ordinary, expected activity with no fraud indicators",
                },
            },
            "fraud_likelihood": {
                "type": "noul",
                "instructions": "Is this narrative likely to be fraudulent?",
                "criteria": {
                    "yes": "contains fraud indicators worth investigating",
                    "no": "appears to be legitimate activity",
                },
            },
            "severity": {
                "type": "score",
                "instructions": "How severe is the potential loss?",
                "criteria": [
                    "no monetary loss, informational",
                    "small loss, single transaction, isolated",
                    "significant loss or repeated fraudulent attempts",
                    "systemic, affects many accounts at once",
                ],
            },
        },
    },
    {
        "key": "content-moderation",
        "name": "Content moderation",
        "description": (
            "Apply a calibrated guardrail to user-generated content and pick the "
            "sanction. Noul gives the policy trigger, choice gives the action."
        ),
        "tags": ["safety", "moderation", "guardrail"],
        "questions": {
            "violates_policy": {
                "type": "noul",
                "instructions": "Does this content violate the acceptable-use policy?",
                "criteria": {
                    "yes": "targets a person with abuse, shows explicit material, depicts graphic violence, or promotes illegal conduct",
                    "no": "ordinary posts that stay comfortably within the acceptable-use rules",
                },
            },
            "category": {
                "type": "choice",
                "instructions": "Which policy category does it fall under?",
                "criteria": {
                    "harassment": "targeted abuse, threats, or intimidation toward a person",
                    "hate": "attack based on a protected characteristic",
                    "sexual": "explicit sexual content or solicitation",
                    "violence": "graphic violence or incitement",
                    "spam": "bulk promotion, scams, or platform manipulation",
                    "none": "no category applies",
                },
            },
            "sanction": {
                "type": "choice",
                "instructions": "What action should be taken?",
                "criteria": {
                    "allow": "publish as-is",
                    "warn": "publish but add a sensitive-content interstitial",
                    "review": "hold for a human moderator",
                    "remove": "remove the content",
                    "ban": "remove and suspend the account",
                },
            },
        },
    },
    {
        "key": "support-priority",
        "name": "Support ticket priority",
        "description": (
            "A single-ordinal-rubric example. Useful as the baseline in an A/B test "
            "when you are tuning rubric wording."
        ),
        "tags": ["support", "starter"],
        "questions": {
            "business_impact": {
                "type": "score",
                "instructions": "How much business impact does this ticket describe?",
                "criteria": [
                    "informational question, cosmetic, nothing blocked",
                    "one person slowed down, workaround available",
                    "whole team blocked, deadline at risk",
                    "production down, revenue actively lost, outage",
                ],
            }
        },
    },
]


def seed_builtin_templates(db: Session, *, commit: bool = True) -> list[EvalSchema]:
    """Insert the built-in templates if they are missing. Idempotent."""
    created: list[EvalSchema] = []
    for template in TEMPLATE_DEFINITIONS:
        existing = get_schema_by_key(db, template["key"])
        if existing is not None:
            continue
        created.append(
            create_schema(
                db,
                key=template["key"],
                name=template["name"],
                description=template["description"],
                questions=template["questions"],
                tags=template["tags"],
                is_template=True,
                note="Built-in template",
                commit=False,
            )
        )
    if commit and created:
        db.commit()
        for schema in created:
            db.refresh(schema)
    return created
