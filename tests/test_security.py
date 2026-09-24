"""Security-relevant behaviour: ingestion auth, upload limits, template safety."""

from __future__ import annotations

import dataclasses

import pytest

from app.config import get_settings


@pytest.fixture
def ingest_token(monkeypatch):
    """Turn the shared ingestion token on for one test."""

    def install(token: str = "s3cret") -> str:
        settings = dataclasses.replace(get_settings(), ingest_token=token)
        monkeypatch.setattr("app.deps.get_settings", lambda: settings)
        return token

    return install


def _a_workflow(client, triage_schema) -> dict:
    return client.post(
        "/api/workflows",
        json={
            "name": "Guarded",
            "schema_id": triage_schema["id"],
            "decision_question": "department",
        },
    ).json()


def test_ingestion_is_open_when_no_token_is_configured(client, triage_schema):
    workflow = _a_workflow(client, triage_schema)
    assert client.post(f"/api/ingest/{workflow['key']}", json={"text": "hi"}).status_code == 200


def test_ingestion_requires_the_token_when_one_is_configured(client, triage_schema, ingest_token):
    ingest_token()
    workflow = _a_workflow(client, triage_schema)

    missing = client.post(f"/api/ingest/{workflow['key']}", json={"text": "hi"})
    assert missing.status_code == 401

    wrong = client.post(
        f"/api/ingest/{workflow['key']}",
        json={"text": "hi"},
        headers={"X-Ingest-Token": "nope"},
    )
    assert wrong.status_code == 401

    right = client.post(
        f"/api/ingest/{workflow['key']}",
        json={"text": "hi"},
        headers={"X-Ingest-Token": "s3cret"},
    )
    assert right.status_code == 200


def test_the_token_guard_covers_every_ingestion_door(client, triage_schema, ingest_token):
    ingest_token()
    workflow = _a_workflow(client, triage_schema)

    assert client.post(f"/api/ingest/{workflow['key']}/text", content=b"hi").status_code == 401
    assert client.post(
        f"/api/ingest/email/{workflow['key']}", json={"body": "hi"}
    ).status_code == 401


def test_the_token_guard_does_not_leak_to_other_routers(client, triage_schema, ingest_token):
    ingest_token()
    # The playground stays open: the token protects ingestion, not the whole panel.
    assert client.get("/api/schemas").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_upload_size_limit_is_enforced(client, triage_schema, monkeypatch):
    settings = dataclasses.replace(get_settings(), max_upload_mb=0)
    monkeypatch.setattr("app.routers.bulk.get_settings", lambda: settings)

    body = "subject,body\n" + "".join(f"row {i},body {i}\n" for i in range(50))
    response = client.post(
        "/api/bulk/upload",
        files={"file": ("big.csv", body, "text/csv")},
        data={"schema_id": str(triage_schema["id"])},
    )
    assert response.status_code == 413
    assert "MAX_UPLOAD_MB" in response.json()["detail"]


def test_sql_injection_shaped_input_is_stored_verbatim_and_harmlessly(client, triage_schema):
    payload = "'; DROP TABLE evaluations; --"
    created = client.post(
        "/api/playground/evaluate",
        json={"state": {"body": payload}, "schema_id": triage_schema["id"]},
    )
    assert created.status_code == 200
    assert created.json()["state"]["body"] == payload
    # The table is still there and still queryable through the ORM.
    assert client.get("/api/health").json()["database"]["tables"]["evaluations"] == 1


def test_states_are_not_executed_as_templates(client, triage_schema):
    """A state must never be interpreted as a format string by an action."""
    from app.services import actions

    rendered = actions.render(
        {"detail": "{choice}"},
        {"choice": "{not_a_key}", "not_a_key": "boom"},
    )
    assert rendered == {"detail": "{not_a_key}"}


def test_error_messages_do_not_echo_internal_paths(client):
    response = client.post("/api/playground/evaluate", json={"state": "hi", "schema_id": 1})
    assert response.status_code == 200 or "Traceback" not in response.text
    assert "/Users/" not in response.text
