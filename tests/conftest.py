"""Shared pytest fixtures.

The suite runs hermetically: a throwaway PostgreSQL container, and a deterministic
test double in place of Laya (see :mod:`tests.doubles`). No GPU, no network, no
model weights. ``tests/test_real_laya.py`` covers the adapter against the real
runtime and is deselected by default.

Two things to know:

* **The database is PostgreSQL and there is no alternative.** :mod:`tests.pg`
  starts a container, or uses ``LAYA_STUDIO_TEST_DATABASE_URL`` if you set one.
* **The schema is built from the SQLAlchemy metadata, not from Prisma.** That keeps
  each test fast; ``tests/test_schema_drift.py`` separately proves the metadata and
  Prisma's migrations produce the same schema.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tests.pg import PostgresUnavailable
from tests.pg import start as start_postgres

# --- configure the environment before any app import -------------------------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="laya-studio-tests-"))

try:
    _PG = start_postgres()
except PostgresUnavailable as exc:  # pragma: no cover - environment problem
    pytest.exit(f"\n{exc}", returncode=1)

os.environ["LAYA_STUDIO_ENVIRONMENT"] = "test"
os.environ["LAYA_STUDIO_DATABASE_URL"] = _PG.url
os.environ["LAYA_STUDIO_DATA_DIR"] = str(_TMP_ROOT)
os.environ["LAYA_STUDIO_SECRET_KEY"] = "test-secret"
# Fit temperatures eagerly so calibration behaviour is testable with small fixtures.
os.environ["LAYA_STUDIO_CALIBRATION_MIN_SAMPLES"] = "10"
os.environ["LAYA_STUDIO_CALIBRATION_CACHE_TTL"] = "0"
# Webhooks in tests target loopback servers.
os.environ["LAYA_STUDIO_WEBHOOK_ALLOWED_HOSTS"] = "localhost,127.0.0.1"

from app.config import Settings, get_settings, reload_settings  # noqa: E402
from app.db import (  # noqa: E402
    create_schema,
    drop_schema,
    get_engine,
    session_scope,
    truncate_all,
)
from app.inference import registry as engine_registry  # noqa: E402
from app.services import calibration as calibration_service  # noqa: E402
from app.services import decision_schemas  # noqa: E402
from tests.doubles import LexicalEngine, ScriptedEngine  # noqa: E402


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--real-laya",
        action="store_true",
        default=False,
        help="Run tests that load real Laya checkpoints (slow, downloads weights).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--real-laya"):
        return
    skip = pytest.mark.skip(reason="needs --real-laya (loads real checkpoints)")
    for item in items:
        if "real_laya" in item.keywords:
            item.add_marker(skip)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Tear the container down once the whole session is done."""
    _PG.stop()


@pytest.fixture(scope="session", autouse=True)
def postgres():
    """The session's database, as a session-scoped fixture so tests can depend on it."""
    yield _PG


@pytest.fixture(scope="session", autouse=True)
def _settings():
    reload_settings()
    yield get_settings()


@pytest.fixture(autouse=True)
def engine():
    """Install the lexical test double for every test.

    Individual tests can call ``engine_registry.set_engine(ScriptedEngine(...))``
    to take over; this fixture guarantees the previous one is removed afterwards
    so a double can never leak into another test.
    """
    double = LexicalEngine()
    engine_registry.set_engine(double)
    yield double
    engine_registry.set_engine(None)


@pytest.fixture
def scripted():
    """Swap in a :class:`ScriptedEngine` for one test and return it."""

    def install(**kwargs) -> ScriptedEngine:
        double = ScriptedEngine(**kwargs)
        engine_registry.set_engine(double)
        return double

    return install


@pytest.fixture(scope="session", autouse=True)
def _schema(postgres):
    """Build the schema once for the whole session.

    From the ORM metadata rather than by running Prisma — shelling out to Node for
    every session would be slow, and ``test_schema_drift.py`` proves the two agree.
    """
    engine = get_engine()
    drop_schema(engine)
    create_schema(engine)
    yield engine


@pytest.fixture(autouse=True)
def fresh_db(_schema):
    """Empty every table and re-seed the built-in templates before every test.

    A single ``TRUNCATE ... RESTART IDENTITY CASCADE`` rather than a drop/recreate
    cycle: same isolation, a fraction of the cost.
    """
    truncate_all(_schema)

    with session_scope() as session:
        decision_schemas.seed_builtin_templates(session)

    calibration_service.clear_cache()
    engine_registry.reset_engine_cache()
    yield
    calibration_service.clear_cache()


@pytest.fixture
def empty_db():
    """Run a test against a database with no schema at all."""
    drop_schema(get_engine())
    yield
    create_schema(get_engine())


@pytest.fixture
def client(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def session(fresh_db):
    from app.db import get_session_factory

    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def triage_schema(client):
    """The email-triage built-in template, as the API returns it."""
    schemas = client.get("/api/schemas").json()
    return next(item for item in schemas if item["key"] == "email-triage")


@pytest.fixture
def sample_state() -> dict[str, str]:
    return {
        "subject": "Duplicate charge on invoice #4411",
        "body": "We were billed twice for March. Please refund the duplicate today or we cancel.",
    }


@pytest.fixture
def settings_factory():
    """Build a modified Settings snapshot, for testing config-dependent branches."""

    def build(**overrides) -> Settings:
        import dataclasses

        return dataclasses.replace(get_settings(), **overrides)

    return build


# --- helpers -----------------------------------------------------------------


class FakeProvider:
    """A stand-in generative provider that returns canned JSON."""

    name = "fake"

    def __init__(self, payload: str, *, available: bool = True) -> None:
        self.payload = payload
        self._available = available
        self.calls: list[str] = []

    def available(self) -> bool:
        return self._available

    def complete(self, prompt, *, system=None, max_tokens=512, temperature=0.0):
        from app.llm.base import LLMResponse

        self.calls.append(prompt)
        return LLMResponse(
            text=self.payload, provider=self.name, model="fake-1", latency_ms=1.0
        )

    def describe(self) -> dict:
        return {"provider": self.name, "available": self._available, "model": "fake-1"}


@pytest.fixture
def fake_provider(monkeypatch):
    """Install a fake LLM provider and return a factory for it."""

    def install(payload: str, *, available: bool = True) -> FakeProvider:
        provider = FakeProvider(payload, available=available)
        monkeypatch.setattr("app.llm.service.get_provider", lambda settings=None: provider)
        return provider

    return install
