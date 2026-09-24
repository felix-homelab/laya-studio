"""Playground API: evaluation, routing, linting, history, labelling."""

from __future__ import annotations

import pytest


def test_evaluate_returns_the_full_visualizer_payload(client, triage_schema, sample_state):
    response = client.post(
        "/api/playground/evaluate",
        json={"state": sample_state, "schema_id": triage_schema["id"]},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["id"] is not None
    assert body["top_choice"]
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["engine"] == "lexical"
    assert body["routing"]["model"] == "english"
    assert len(body["question_results"]) == 4

    # choice and score carry a distribution; noul is a single calibrated
    # probability and correctly has none.
    for entry in body["question_results"]:
        assert entry["band"] in ("act", "verify", "escalate", "unknown")
        if entry["type"] in ("choice", "score"):
            assert entry["breakdown"], entry["question"]
            probabilities = [item["probability"] for item in entry["breakdown"]]
            assert sum(probabilities) == pytest.approx(1.0, rel=1e-5)
        else:
            assert entry["noul"] is not None
            assert entry["breakdown"] == []

    ranked = {entry["question"]: entry["breakdown"] for entry in body["question_results"]}
    assert ranked["department"][0]["is_top"] is True
    assert ranked["department"][0]["probability"] == max(
        item["probability"] for item in ranked["department"]
    )

    assert body["engine"] == "lexical"


def test_evaluate_routes_non_latin_to_the_multilingual_checkpoint(client, triage_schema):
    response = client.post(
        "/api/playground/evaluate",
        json={"state": {"body": "同じ月に二回請求されています。返金してください。"}, "schema_id": triage_schema["id"]},
    )
    assert response.status_code == 200
    assert response.json()["routing"]["model"] == "multilingual"


def test_evaluate_accepts_an_inline_question_bundle(client):
    response = client.post(
        "/api/playground/evaluate",
        json={
            "state": "the invoice was charged twice",
            "questions": {
                "topic": {
                    "type": "choice",
                    "instructions": "Which topic?",
                    "criteria": {"billing": "invoices and charges", "other": "anything else"},
                }
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["top_choice"] == "billing"


def test_evaluate_rejects_a_request_with_no_questions_at_all(client):
    response = client.post("/api/playground/evaluate", json={"state": "hello"})
    assert response.status_code == 422


def test_evaluate_rejects_an_invalid_bundle(client):
    response = client.post(
        "/api/playground/evaluate",
        json={"state": "hello", "questions": {"q": {"type": "choice", "criteria": {"only": "one"}}}},
    )
    assert response.status_code == 422
    assert "at least two labels" in response.json()["detail"]


def test_evaluate_can_skip_persistence(client, triage_schema, sample_state):
    response = client.post(
        "/api/playground/evaluate",
        json={"state": sample_state, "schema_id": triage_schema["id"], "persist": False},
    )
    assert response.status_code == 200
    assert response.json()["id"] is None
    assert client.get("/api/playground/history").json() == []


def test_route_endpoint_explains_the_checkpoint_without_scoring(client):
    response = client.post("/api/playground/route", json={"state": {"text": "שלום עולם"}})
    assert response.status_code == 200
    routing = response.json()["routing"]
    assert routing["model"] == "multilingual"
    assert "hebrew" in routing["reason"]


def test_lint_endpoint_returns_findings_and_an_ok_flag(client):
    response = client.post(
        "/api/playground/lint",
        json={
            "questions": {
                "q": {
                    "type": "choice",
                    "criteria": {f"l{i}": f"d{i}" for i in range(25)},
                    "instructions": "pick",
                }
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True  # warnings only
    assert any(finding["severity"] == "warning" for finding in body["findings"])


def test_lint_endpoint_flags_errors(client):
    response = client.post(
        "/api/playground/lint", json={"questions": {"q": {"type": "bogus"}}}
    )
    assert response.json()["ok"] is False


def test_labelling_an_evaluation_records_ground_truth(client, triage_schema, sample_state):
    created = client.post(
        "/api/playground/evaluate",
        json={"state": sample_state, "schema_id": triage_schema["id"]},
    ).json()

    labelled = client.post(
        f"/api/playground/evaluations/{created['id']}/label",
        json={"label": "billing", "label_source": "human"},
    )
    assert labelled.status_code == 200
    assert labelled.json()["label"] == "billing"


def test_labelling_a_missing_evaluation_is_a_404(client):
    assert client.post("/api/playground/evaluations/9999/label", json={"label": "x"}).status_code == 404


def test_history_is_newest_first_and_scoped_to_the_playground(client, triage_schema, sample_state):
    for index in range(3):
        client.post(
            "/api/playground/evaluate",
            json={
                "state": {**sample_state, "subject": f"{sample_state['subject']} #{index}"},
                "schema_id": triage_schema["id"],
            },
        )

    history = client.get("/api/playground/history?limit=10").json()
    assert len(history) == 3
    assert all(row["source"] == "playground" for row in history)
    timestamps = [row["created_at"] for row in history]
    assert timestamps == sorted(timestamps, reverse=True)


def test_unknown_schema_id_is_a_404(client):
    response = client.post(
        "/api/playground/evaluate", json={"state": "hi", "schema_id": 4242}
    )
    assert response.status_code == 404
