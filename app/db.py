"""Database engine and session factory.

**PostgreSQL only.** There is no SQLite path: the application expects a real
server, and the schema is owned by Prisma's migrations rather than created from
these models (see :mod:`prisma` and ``docs/data-model.md``).

Two consequences worth knowing:

* ``SQLAlchemy`` here is a *query* layer, not a schema authority. ``Base.metadata``
  is kept accurate because the drift test compares it against what the migrations
  actually produce, but nothing calls ``create_all`` outside the test suite.
* The engine is created lazily so tests can point ``LAYA_STUDIO_DATABASE_URL`` at
  a throwaway server before the first session opens.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


class SchemaNotMigrated(RuntimeError):
    """The database is reachable but does not have the expected tables.

    Raised with instructions rather than letting a query fail with
    ``UndefinedTable`` somewhere deep in a request.
    """


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_session_factory_engine: Engine | None = None
_engine_url: str | None = None


def normalize_database_url(url: str) -> str:
    """Coerce a Postgres DSN into the form SQLAlchemy needs.

    Accepts what people actually paste:

    * ``postgresql://`` and ``postgres://`` → ``postgresql+psycopg://``
    * ``postgresql+psycopg2://`` → ``postgresql+psycopg://`` (one driver, not two)

    Anything that is not Postgres is rejected loudly. Refusing here is much better
    than a confusing failure later, and it makes "we are Postgres-only" a property
    of the code rather than a convention.
    """
    if not url:
        raise ValueError(
            "LAYA_STUDIO_DATABASE_URL is empty. Laya Studio requires PostgreSQL; "
            "see docs/deployment.md."
        )

    scheme, _, rest = url.partition("://")
    if not rest:
        raise ValueError(f"Malformed DATABASE_URL {url!r}: expected postgresql://…")

    base = scheme.split("+", 1)[0]
    if base not in ("postgresql", "postgres"):
        raise ValueError(
            f"Unsupported database scheme {scheme!r}. Laya Studio is PostgreSQL-only; "
            "SQLite and other backends are not supported."
        )

    return f"postgresql+psycopg://{rest}"


def get_engine() -> Engine:
    """Return the process-wide :class:`~sqlalchemy.engine.Engine`."""
    global _engine, _engine_url
    url = normalize_database_url(get_settings().database_url)
    if _engine is not None and _engine_url == url:
        return _engine

    if _engine is not None:
        _engine.dispose()

    _engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        # FastAPI runs sync endpoints on a thread pool, so connections move between
        # threads. SQLAlchemy's pool is thread-safe; this only bounds how long a
        # pool checkout waits when every connection is busy.
        pool_timeout=30,
    )
    _engine_url = url
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory, _session_factory_engine
    engine = get_engine()
    if _session_factory is None or _session_factory_engine is not engine:
        _session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            future=True,
        )
        _session_factory_engine = engine
    return _session_factory


def reset_engine() -> None:
    """Dispose and forget the current engine."""
    global _engine, _session_factory, _session_factory_engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _session_factory_engine = None
    _engine_url = None


def expected_tables() -> set[str]:
    """Every table the ORM expects to find."""
    from . import models  # noqa: F401 - register the mappings

    return set(Base.metadata.tables)


def verify_schema(engine: Engine | None = None) -> None:
    """Fail with instructions if the migrations have not been applied.

    The schema belongs to Prisma, so the application cannot create it. Rather than
    letting the first query raise ``UndefinedTable`` inside a request, the startup
    path checks once and says exactly what to run.
    """
    engine = engine or get_engine()
    present = set(inspect(engine).get_table_names())
    missing = sorted(expected_tables() - present)

    if missing:
        raise SchemaNotMigrated(
            "The database is reachable but has not been migrated.\n"
            f"Missing table(s): {', '.join(missing)}\n\n"
            "The schema is owned by Prisma's migrations, not by the application.\n"
            "Apply them with:\n"
            "    make db-migrate          # npx prisma migrate deploy\n"
            "or, for a development database:\n"
            "    make db-migrate-dev      # npx prisma migrate dev\n"
        )


def ping(engine: Engine | None = None) -> bool:
    """True when the server answers a trivial query."""
    try:
        with (engine or get_engine()).connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - callers report the failure themselves
        return False


def create_schema(engine: Engine | None = None) -> None:
    """Create every table from the ORM metadata.

    **Test-only.** The real schema comes from Prisma's migrations; this exists so
    the fast test suite does not have to shell out to Node for every session, and
    ``tests/test_schema_drift.py`` proves the two agree.
    """
    from . import models  # noqa: F401 - register the mappings

    Base.metadata.create_all(bind=engine or get_engine())


def drop_schema(engine: Engine | None = None) -> None:
    """Drop every table. Test-only, mirroring :func:`create_schema`."""
    from . import models  # noqa: F401

    Base.metadata.drop_all(bind=engine or get_engine())


def truncate_all(engine: Engine | None = None) -> None:
    """Empty every table and reset the identity sequences.

    Test-only. ``TRUNCATE ... RESTART IDENTITY CASCADE`` in one statement is orders
    of magnitude cheaper than dropping and recreating twelve tables, and it leaves
    the schema — which Prisma owns — untouched.
    """
    from . import models  # noqa: F401

    tables = ", ".join(f'"{name}"' for name in sorted(Base.metadata.tables))
    if not tables:
        return
    with (engine or get_engine()).begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background workers and scripts."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
