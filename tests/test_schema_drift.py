"""The schema Prisma's migrations produce must match ``app/models.py``.

This is the guard the whole arrangement rests on. PostgreSQL DDL belongs to
``prisma/schema.prisma``; the application queries those tables through SQLAlchemy.
Two definitions of one schema will drift unless something checks, so this migrates
a **fresh database with Prisma** and compares the result, column by column, against
the ORM metadata.

It is slower than the rest of the suite — it shells out to Node and waits for
``prisma migrate deploy`` — but it is the only thing standing between "Prisma owns
the schema" and "nobody knows what the schema is".

Deselect with ``-m 'not drift'`` when iterating.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql

from app.db import Base, normalize_database_url

pytestmark = pytest.mark.drift

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PG_DIALECT = postgresql.dialect()

#: Prisma's own bookkeeping table; not part of the application's schema.
PRISMA_TABLE = "_prisma_migrations"


def _prisma_available() -> bool:
    return shutil.which("npx") is not None and os.path.isdir(os.path.join(REPO_ROOT, "node_modules"))


@pytest.fixture(scope="module")
def migrated_url(postgres):
    """A database whose schema was created by ``prisma migrate deploy``.

    Uses its own database so it cannot disturb the session's schema.
    """
    if not _prisma_available():
        pytest.skip("npx (or node_modules/prisma) is unavailable; cannot run the migrations")

    admin = create_engine(normalize_database_url(postgres.url), isolation_level="AUTOCOMMIT")
    database = f"laya_drift_{uuid.uuid4().hex[:10]}"
    with admin.connect() as connection:
        try:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"cannot create a scratch database on the test server: {exc}")

    url = postgres.url.rsplit("/", 1)[0] + f"/{database}"

    environment = dict(os.environ)
    # Prisma wants a plain postgresql:// DSN; it cannot parse SQLAlchemy's +driver.
    environment["LAYA_STUDIO_DATABASE_URL"] = url
    result = subprocess.run(
        ["npx", "prisma", "migrate", "deploy"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}"'))
        pytest.fail(f"prisma migrate deploy failed:\n{result.stdout}\n{result.stderr}")

    yield url

    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    admin.dispose()


def _live_type(column: dict) -> str:
    """Normalise the inspector's representation of a column type."""
    declared = str(column["type"]).upper()
    # PostgreSQL has no separate "timestamp with time zone" class in the inspector;
    # the flag carries the difference and dropping it would hide a real bug.
    if declared == "TIMESTAMP" and getattr(column["type"], "timezone", False):
        return "TIMESTAMP WITH TIME ZONE"
    return declared


def _model_type(column) -> str:
    return str(column.type.compile(dialect=PG_DIALECT)).upper()


def _live_columns(inspector, table: str) -> dict[str, tuple[str, bool]]:
    return {c["name"]: (_live_type(c), c["nullable"]) for c in inspector.get_columns(table)}


def _model_columns(table) -> dict[str, tuple[str, bool]]:
    return {c.name: (_model_type(c), c.nullable) for c in table.columns}


def _live_unique(inspector, table: str) -> set[tuple[str, ...]]:
    unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(table)}
    unique |= {tuple(i["column_names"]) for i in inspector.get_indexes(table) if i["unique"]}
    return unique


def _model_unique(table) -> set[tuple[str, ...]]:
    from sqlalchemy import UniqueConstraint

    unique = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    unique |= {tuple(c.name for c in index.columns) for index in table.indexes if index.unique}
    return unique


def _live_foreign_keys(inspector, table: str) -> set[tuple]:
    return {
        (
            tuple(fk["constrained_columns"]),
            fk["referred_table"],
            (fk.get("options") or {}).get("ondelete"),
        )
        for fk in inspector.get_foreign_keys(table)
    }


def _model_foreign_keys(table) -> set[tuple]:
    keys = set()
    for constraint in table.foreign_key_constraints:
        referred = next(iter(constraint.elements)).column.table.name
        keys.add((tuple(constraint.columns.keys()), referred, constraint.ondelete))
    return keys


def _live_indexes(inspector, table: str) -> dict[tuple[str, ...], str]:
    """Index column-tuples mapped to their name, excluding uniqueness (checked above)."""
    return {
        tuple(index["column_names"]): index["name"]
        for index in inspector.get_indexes(table)
        if not index["unique"]
    }


def _model_indexes(table) -> dict[tuple[str, ...], str]:
    return {
        tuple(c.name for c in index.columns): index.name
        for index in table.indexes
        if not index.unique
    }


def test_prisma_migrations_match_the_sqlalchemy_models(migrated_url):
    """The one assertion that keeps two schema definitions honest."""
    inspector = inspect(create_engine(normalize_database_url(migrated_url)))

    live_tables = set(inspector.get_table_names()) - {PRISMA_TABLE}
    model_tables = set(Base.metadata.tables)

    assert live_tables == model_tables, (
        f"Tables only the migrations create: {sorted(live_tables - model_tables)}\n"
        f"Tables only the models define: {sorted(model_tables - live_tables)}"
    )

    problems: list[str] = []

    for name in sorted(model_tables):
        table = Base.metadata.tables[name]

        live_columns = _live_columns(inspector, name)
        model_columns = _model_columns(table)

        for column in sorted(set(live_columns) | set(model_columns)):
            if column not in live_columns:
                problems.append(f"{name}.{column}: in the models, missing from the migrations")
            elif column not in model_columns:
                problems.append(f"{name}.{column}: in the migrations, missing from the models")
            elif live_columns[column] != model_columns[column]:
                problems.append(
                    f"{name}.{column}: type/nullability differ — "
                    f"migrations={live_columns[column]} models={model_columns[column]}"
                )

        live_pk = tuple(inspector.get_pk_constraint(name)["constrained_columns"])
        model_pk = tuple(c.name for c in table.primary_key.columns)
        if live_pk != model_pk:
            problems.append(f"{name}: primary key differs — migrations={live_pk} models={model_pk}")

        if _live_unique(inspector, name) != _model_unique(table):
            problems.append(
                f"{name}: unique constraints differ — "
                f"migrations={sorted(_live_unique(inspector, name))} "
                f"models={sorted(_model_unique(table))}"
            )

        if _live_foreign_keys(inspector, name) != _model_foreign_keys(table):
            problems.append(
                f"{name}: foreign keys differ — "
                f"migrations={sorted(_live_foreign_keys(inspector, name))} "
                f"models={sorted(_model_foreign_keys(table))}"
            )

        # Compare index *names* as well as columns: the migrations fix them with
        # `map:` so the two stay greppable, and a rename is worth noticing.
        live_indexes = _live_indexes(inspector, name)
        model_indexes = _model_indexes(table)
        if live_indexes != model_indexes:
            problems.append(
                f"{name}: indexes differ — migrations={dict(sorted(live_indexes.items()))} "
                f"models={dict(sorted(model_indexes.items()))}"
            )

    assert not problems, (
        "The Prisma migrations and app/models.py have drifted apart.\n"
        "Decide which is wrong, fix it, and regenerate the migration with\n"
        "`npx prisma migrate dev --name <change>` if the schema itself changed:\n\n  "
        + "\n  ".join(problems)
    )


def test_the_migration_history_is_present(migrated_url):
    """Prisma must have actually recorded the migrations, not just run the SQL."""
    inspector = inspect(create_engine(normalize_database_url(migrated_url)))
    assert PRISMA_TABLE in inspector.get_table_names()

    with create_engine(normalize_database_url(migrated_url)).connect() as connection:
        applied = connection.execute(
            text(f"SELECT migration_name, finished_at FROM {PRISMA_TABLE} ORDER BY started_at")
        ).fetchall()

    assert applied, "no migrations recorded"
    assert all(row[1] is not None for row in applied), "a migration did not finish"
    assert any("init" in row[0] for row in applied)
