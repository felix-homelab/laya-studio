"""Engine startup, and what the panel does while Laya is still loading.

This is the behaviour that matters on a first `docker compose up`: the checkpoints
can take minutes to download, and uvicorn does not bind its socket until the ASGI
lifespan returns. Building the engine inline would therefore make the panel refuse
every connection for the whole download. These tests pin the alternative — the
panel is up immediately, and says so.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.inference import registry
from app.inference.base import EngineNotReady, EngineUnavailable
from tests.doubles import LexicalEngine


@pytest.fixture
def controlled(client, monkeypatch):
    """Drive the real registry with the builder under test control.

    Depends on ``client`` so the app has already started with its test double in
    place; this fixture then clears the override and takes over the real path.
    """
    gate = threading.Event()
    outcome: dict = {"error": None, "engine": None}

    def build(settings=None):
        gate.wait(timeout=10)
        if outcome["error"] is not None:
            raise outcome["error"]
        return outcome["engine"] or LexicalEngine()

    monkeypatch.setattr(registry, "build_engine", build)
    registry.set_engine(None)
    registry.reset_engine_cache()
    yield registry, gate, outcome
    gate.set()
    registry.set_engine(None)
    registry.reset_engine_cache()


def test_state_is_pending_before_anything_starts(controlled):
    reg, _gate, _outcome = controlled
    assert reg.engine_status()["state"] == "pending"
    assert reg.engine_status()["ready"] is False


def test_get_engine_refuses_while_loading_rather_than_blocking(controlled):
    reg, gate, _outcome = controlled
    reg.start_engine(background=True)

    assert reg.engine_status()["state"] == "loading"
    with pytest.raises(EngineNotReady) as excinfo:
        reg.get_engine()
    assert excinfo.value.retry_after >= 1

    gate.set()
    assert reg.wait_for_engine(5) is True
    assert reg.engine_status()["state"] == "ready"
    assert reg.get_engine().name == "lexical"


def test_the_panel_serves_while_the_engine_loads(client, controlled):
    """The whole point: pages work, the health endpoint answers, nothing hangs."""
    reg, gate, _outcome = controlled
    reg.start_engine(background=True)

    # Pages render, and say what is happening.
    for path in ("/", "/schemas", "/bulk", "/ab", "/workflows", "/analytics", "/review"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert "Laya is still loading" in client.get("/").text

    # Health is liveness: 200, with the state visible.
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "starting"
    assert body["engine"]["state"] == "loading"
    assert body["engine"]["ready"] is False

    gate.set()
    reg.wait_for_engine(5)
    assert client.get("/api/health").json()["status"] == "ok"
    assert "Laya is still loading" not in client.get("/").text


def test_decisions_return_503_with_retry_after_while_loading(client, controlled):
    reg, gate, _outcome = controlled
    reg.start_engine(background=True)

    response = client.post(
        "/api/playground/evaluate",
        json={"state": "hello", "schema_id": 1},
    )
    assert response.status_code == 503
    assert response.headers["Retry-After"] == str(response.json()["retry_after"])
    assert response.json()["state"] == "loading"
    assert "still loading" in response.json()["detail"]

    # Non-decision endpoints keep working.
    assert client.get("/api/schemas").status_code == 200

    gate.set()
    reg.wait_for_engine(5)
    assert client.post(
        "/api/playground/evaluate", json={"state": "hello", "schema_id": 1}
    ).status_code == 200


def test_a_bulk_job_aborts_when_the_engine_is_not_ready(client, controlled, triage_schema):
    """A batch must report the cause, not "0 rows scored".

    Per-row isolation is right for a malformed row and wrong for a missing engine —
    failing 5,000 rows individually would bury the reason.
    """
    from app.db import get_session_factory

    reg, gate, _outcome = controlled
    reg.start_engine(background=True)

    csv = "subject,body,truth\nRefund,We were billed twice,billing\n"
    job = client.post(
        "/api/bulk/upload",
        files={"file": ("rows.csv", csv, "text/csv")},
        data={"schema_id": str(triage_schema["id"]), "label_column": "truth"},
    )

    assert job.status_code == 503
    assert "still loading" in job.json()["detail"]

    # And nothing was left behind pretending to be a finished job.
    session = get_session_factory()()
    try:
        from app.models import BulkJob

        stored = session.query(BulkJob).order_by(BulkJob.id.desc()).first()
        if stored is not None:
            assert stored.status == "failed"
            assert stored.error
    finally:
        session.close()

    gate.set()
    reg.wait_for_engine(5)
    assert client.post(
        "/api/bulk/upload",
        files={"file": ("rows.csv", csv, "text/csv")},
        data={"schema_id": str(triage_schema["id"]), "label_column": "truth"},
    ).status_code == 201


def test_readiness_probe_gates_on_the_engine(client, controlled):
    reg, gate, _outcome = controlled
    reg.start_engine(background=True)

    not_ready = client.get("/api/ready")
    assert not_ready.status_code == 503
    assert not_ready.json()["ready"] is False
    assert not_ready.headers["Retry-After"] == "10"

    gate.set()
    reg.wait_for_engine(5)
    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


def test_a_broken_runtime_is_reported_as_degraded_not_loading(client, controlled):
    reg, gate, outcome = controlled
    outcome["error"] = EngineUnavailable("The `laya` package is not importable.")
    reg.start_engine(background=True)
    gate.set()
    assert reg.wait_for_engine(5) is False

    health = client.get("/api/health")
    assert health.status_code == 200  # the process is up...
    assert health.json()["status"] == "degraded"  # ...but it cannot serve decisions
    assert "not importable" in health.json()["engine"]["error"]

    # A permanent failure is not a "come back later".
    with pytest.raises(EngineUnavailable):
        reg.get_engine()
    assert client.get("/api/ready").status_code == 503
    assert "could not be loaded" in client.get("/").text


def test_a_permanent_failure_returns_503_not_an_opaque_500(client, controlled):
    """A broken runtime must explain itself over HTTP, not raise a bare RuntimeError."""
    reg, gate, outcome = controlled
    outcome["error"] = EngineUnavailable("The `laya` package is not importable.")
    reg.start_engine(background=True)
    gate.set()
    assert reg.wait_for_engine(5) is False

    response = client.post("/api/playground/evaluate", json={"state": "hi", "schema_id": 1})
    assert response.status_code == 503
    body = response.json()
    assert body["state"] == "failed"
    assert "not importable" in body["detail"]


def test_require_engine_blocks_until_ready(controlled):
    """The CLI and scripts want to wait; the web app is the one that must not."""
    reg, gate, _outcome = controlled
    reg.start_engine(background=True)

    released: list[bool] = []

    def waiter():
        reg.require_engine(timeout=10)
        released.append(True)

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert released == []  # still blocked

    gate.set()
    thread.join(timeout=10)
    assert released == [True]


def test_start_engine_is_idempotent(controlled):
    reg, gate, _outcome = controlled
    for _ in range(5):
        reg.start_engine(background=True)
    gate.set()
    assert reg.wait_for_engine(5) is True
    assert reg.engine_status()["state"] == "ready"


def test_a_settings_change_rebuilds_rather_than_reusing_a_stale_engine(controlled):
    reg, gate, _outcome = controlled
    gate.set()
    reg.start_engine(background=True)
    assert reg.wait_for_engine(5) is True
    assert reg.get_engine() is not None

    reg.reset_engine_cache()
    assert reg.engine_status()["state"] == "pending"
    with pytest.raises(EngineNotReady):
        reg.get_engine()
