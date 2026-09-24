"""Review queue: claiming, resolving, and writing ground truth back."""

from __future__ import annotations

import pytest


def queue_an_item(client, triage_schema, text: str = "zzz qqq wwww") -> dict:
    workflow = client.post(
        "/api/workflows",
        json={
            "name": f"Queue filler {text[:6]}",
            "schema_id": triage_schema["id"],
            "decision_question": "department",
            "steps": [{"on": "review", "kind": "review", "config": {"priority": 10}}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/run", json={"text": text})
    items = client.get(f"/api/review/items?status=open&workflow_id={workflow['id']}").json()
    assert items, "the low-confidence state should have been queued"
    return items[-1]


def test_stats_are_reported_for_an_empty_queue(client):
    stats = client.get("/api/review/stats").json()
    assert stats["total"] == 0
    assert stats["open"] == 0
    assert stats["correction_rate"] is None


def test_a_low_confidence_run_creates_exactly_one_item(client, triage_schema):
    item = queue_an_item(client, triage_schema)
    assert item["status"] == "pending"
    assert item["reason"] == "low_confidence"
    assert item["model_choice"]
    assert item["alternatives"], "the reviewer needs to see what else was considered"
    assert client.get("/api/review/items?status=open").json().__len__() == 1


def test_the_same_evaluation_is_not_queued_twice(client, triage_schema):
    from app.db import get_session_factory
    from app.services import review

    item = queue_an_item(client, triage_schema)

    db = get_session_factory()()
    try:
        again = review.enqueue(
            db,
            state=item["state"],
            state_text=item["state_text"],
            evaluation_id=item["evaluation_id"],
            reason="low_confidence",
        )
        assert again.id == item["id"]
        assert db.query(type(again)).count() == 1
    finally:
        db.close()


def test_claiming_moves_an_item_into_review(client, triage_schema):
    item = queue_an_item(client, triage_schema)
    claimed = client.post(f"/api/review/items/{item['id']}/claim", json={"assignee": "dana"})
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "in_review"
    assert claimed.json()["assignee"] == "dana"


def test_approving_writes_the_models_answer_as_ground_truth(client, triage_schema):
    item = queue_an_item(client, triage_schema)
    resolved = client.post(
        f"/api/review/items/{item['id']}/resolve",
        json={"status": "approved", "notes": "model was right"},
    ).json()

    assert resolved["status"] == "approved"
    assert resolved["resolved_at"] is not None

    evaluation = client.get(f"/api/playground/evaluations/{item['evaluation_id']}").json()
    assert evaluation["label"] == item["model_choice"]


def test_correcting_writes_the_human_answer_and_requires_a_label(client, triage_schema):
    item = queue_an_item(client, triage_schema)

    missing = client.post(f"/api/review/items/{item['id']}/resolve", json={"status": "corrected"})
    assert missing.status_code == 422

    resolved = client.post(
        f"/api/review/items/{item['id']}/resolve",
        json={"status": "corrected", "corrected_label": "sales", "notes": "not billing at all"},
    ).json()
    assert resolved["corrected_label"] == "sales"

    evaluation = client.get(f"/api/playground/evaluations/{item['evaluation_id']}").json()
    assert evaluation["label"] == "sales"


def test_rejecting_writes_no_label(client, triage_schema):
    item = queue_an_item(client, triage_schema)
    client.post(f"/api/review/items/{item['id']}/resolve", json={"status": "rejected", "notes": "not a decision"})

    evaluation = client.get(f"/api/playground/evaluations/{item['evaluation_id']}").json()
    assert evaluation["label"] is None


def test_unknown_status_is_rejected(client, triage_schema):
    item = queue_an_item(client, triage_schema)
    response = client.post(f"/api/review/items/{item['id']}/resolve", json={"status": "maybe"})
    assert response.status_code == 422


def test_reviewed_items_feed_the_correction_rate(client, triage_schema):
    first = queue_an_item(client, triage_schema, "zzz qqq")
    second = queue_an_item(client, triage_schema, "qqq zzz")

    client.post(f"/api/review/items/{first['id']}/resolve", json={"status": "approved"})
    client.post(
        f"/api/review/items/{second['id']}/resolve",
        json={"status": "corrected", "corrected_label": "other"},
    )

    stats = client.get("/api/review/stats").json()
    assert stats["reviewed"] == 2
    assert stats["by_status"]["corrected"] == 1
    assert stats["correction_rate"] == pytest.approx(0.5)


def test_filtering_and_searching(client, triage_schema):
    item = queue_an_item(client, triage_schema, "a very distinctive complaint about billing")

    assert client.get("/api/review/items?status=pending").json()
    assert client.get("/api/review/items?status=approved").json() == []
    assert client.get("/api/review/items?search=distinctive").json()
    assert client.get("/api/review/items?search=absent-term").json() == []
    assert client.get(f"/api/review/items?workflow_id={item['workflow_id']}").json()


def test_retry_reruns_the_owning_workflow(client, triage_schema):
    item = queue_an_item(client, triage_schema)
    response = client.post(f"/api/review/items/{item['id']}/retry")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["route"] in ("auto", "verify", "review")


def test_retry_without_a_workflow_is_a_conflict(client, triage_schema):
    from app.db import get_session_factory
    from app.services import review

    db = get_session_factory()()
    try:
        orphan = review.enqueue(db, state={"text": "x"}, state_text="x")
    finally:
        db.close()

    response = client.post(f"/api/review/items/{orphan.id}/retry")
    assert response.status_code == 409


def test_missing_item_is_a_404(client):
    assert client.get("/api/review/items/999").status_code == 404
    assert client.post("/api/review/items/999/claim", json={"assignee": "x"}).status_code == 404
