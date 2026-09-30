"""Schema API: CRUD, linting, revision minting and template protection."""

from __future__ import annotations


def test_templates_are_seeded_on_startup(client):
    keys = {schema["key"] for schema in client.get("/api/schemas").json()}
    assert {"email-triage", "fraud-detection", "content-moderation", "support-priority"} <= keys


def test_create_read_update_delete_round_trip(client):
    payload = {
        "name": "Sentiment check",
        "description": "A tiny schema",
        "tags": ["demo"],
        "questions": {
            "polarity": {
                "type": "choice",
                "instructions": "Positive or negative?",
                "criteria": {"positive": "praise and thanks", "negative": "complaints and anger"},
            }
        },
    }

    created = client.post("/api/schemas", json=payload)
    assert created.status_code == 201
    schema = created.json()
    assert schema["key"] == "sentiment-check"
    assert schema["primitive"] == "choice"
    assert schema["current_revision"] == 1

    fetched = client.get(f"/api/schemas/{schema['id']}").json()
    assert fetched["definition"]["questions"]["polarity"]["type"] == "choice"

    updated = client.put(
        f"/api/schemas/{schema['id']}",
        json={"name": "Sentiment check v2", "note": "renamed"},
    )
    # A metadata-only edit must NOT mint a revision.
    assert updated.json()["current_revision"] == 1
    assert updated.json()["name"] == "Sentiment check v2"

    assert client.delete(f"/api/schemas/{schema['id']}").status_code == 204
    assert client.get(f"/api/schemas/{schema['id']}").status_code == 404


def test_changing_questions_mints_an_immutable_revision(client):
    created = client.post(
        "/api/schemas",
        json={
            "name": "Rev test",
            "questions": {
                "q": {"type": "noul", "instructions": "Is it true?", "criteria": {"yes": "yes it is", "no": "no it is not"}}
            },
        },
    ).json()

    client.put(
        f"/api/schemas/{created['id']}",
        json={
            "questions": {
                "q": {"type": "noul", "instructions": "Is this genuinely true?", "criteria": {"yes": "yes it is", "no": "no it is not"}}
            },
            "note": "reworded the instruction",
        },
    )

    revisions = client.get(f"/api/schemas/{created['id']}/revisions").json()
    assert [item["revision"] for item in revisions] == [2, 1]
    # The old revision keeps its original wording.
    original = next(item for item in revisions if item["revision"] == 1)
    assert original["definition"]["questions"]["q"]["instructions"] == "Is it true?"


def test_invalid_questions_are_rejected_with_an_explanation(client):
    response = client.post(
        "/api/schemas",
        json={"name": "Broken", "questions": {"q": {"type": "choice", "criteria": {"solo": "one"}}}},
    )
    assert response.status_code == 422
    assert "at least two labels" in response.json()["detail"]


def test_built_in_templates_cannot_be_deleted(client, triage_schema):
    response = client.delete(f"/api/schemas/{triage_schema['id']}")
    assert response.status_code == 409
    assert "Fork" in response.json()["detail"]


def test_forking_a_template_produces_an_editable_copy(client, triage_schema):
    forked = client.post(
        f"/api/schemas/{triage_schema['id']}/fork", params={"name": "My triage"}
    )
    assert forked.status_code == 201
    body = forked.json()
    assert body["is_template"] is False
    assert body["key"] == "my-triage"
    original = client.get(f"/api/schemas/{triage_schema['id']}").json()
    assert body["definition"] == original["definition"]
    # And now it *can* be deleted.
    assert client.delete(f"/api/schemas/{body['id']}").status_code == 204


def test_duplicate_names_get_unique_keys(client):
    payload = {"name": "Repeat", "questions": {"q": {"type": "noul", "instructions": "hi"}}}
    first = client.post("/api/schemas", json=payload).json()
    second = client.post("/api/schemas", json=payload).json()
    assert first["key"] == "repeat"
    assert second["key"] == "repeat-2"


def test_an_explicit_duplicate_key_is_a_conflict(client):
    payload = {
        "name": "Keyed",
        "key": "keyed",
        "questions": {"q": {"type": "noul", "instructions": "hi"}},
    }
    assert client.post("/api/schemas", json=payload).status_code == 201
    duplicate = client.post("/api/schemas", json=payload)
    assert duplicate.status_code == 409
    assert "keyed" in duplicate.json()["detail"]


def test_search_and_template_filter(client):
    found = client.get("/api/schemas", params={"search": "triage"}).json()
    assert any(schema["key"] == "email-triage" for schema in found)

    without_templates = client.get("/api/schemas", params={"include_templates": False}).json()
    assert all(schema["is_template"] is False for schema in without_templates)


def test_schema_lint_endpoint(client, triage_schema):
    body = client.get(f"/api/schemas/{triage_schema['id']}/lint").json()
    assert body["ok"] is True


def test_an_evaluation_records_the_revision_that_was_current(client, session):
    """ADR 0004: an evaluation cites the taxonomy it was produced under.

    ``schema_revision_id`` is what makes "isolate the rows from before that edit"
    answerable (docs/analytics-and-calibration.md, "Watching for drift"); the row's
    own copy of ``questions`` stays the authoritative "as sent".
    """
    from sqlalchemy import select

    from app.models import Evaluation

    created = client.post(
        "/api/schemas",
        json={
            "name": "Revision pointer",
            "questions": {
                "topic": {
                    "type": "choice",
                    "instructions": "Which topic?",
                    "criteria": {"billing": "invoices", "other": "anything else"},
                }
            },
        },
    ).json()

    first = client.post(
        "/api/playground/evaluate", json={"state": "billed twice", "schema_id": created["id"]}
    ).json()

    client.put(
        f"/api/schemas/{created['id']}",
        json={
            "questions": {
                "topic": {
                    "type": "choice",
                    "instructions": "Which topic is this about?",
                    "criteria": {"billing": "invoices", "other": "anything else"},
                }
            },
            "note": "reworded the instruction",
        },
    )
    second = client.post(
        "/api/playground/evaluate", json={"state": "billed twice", "schema_id": created["id"]}
    ).json()

    revisions = client.get(f"/api/schemas/{created['id']}/revisions").json()
    revision_id = {item["revision"]: item["id"] for item in revisions}
    assert set(revision_id) == {1, 2}

    pointers = {
        row.id: row.schema_revision_id
        for row in session.scalars(
            select(Evaluation).where(Evaluation.id.in_([first["id"], second["id"]]))
        )
    }

    assert pointers[first["id"]] == revision_id[1]
    assert pointers[second["id"]] == revision_id[2]
    # The whole point of the column: the two rows stay separable by taxonomy.
    assert pointers[first["id"]] != pointers[second["id"]]


def test_an_evaluation_with_no_schema_cites_no_revision(client, session):
    """An inline bundle was produced by no stored taxonomy, so nothing is claimed."""
    from sqlalchemy import select

    from app.models import Evaluation

    created = client.post(
        "/api/playground/evaluate",
        json={
            "state": "billed twice",
            "questions": {
                "topic": {
                    "type": "choice",
                    "instructions": "Which topic?",
                    "criteria": {"billing": "invoices", "other": "anything else"},
                }
            },
        },
    ).json()

    row = session.scalar(select(Evaluation).where(Evaluation.id == created["id"]))
    assert row is not None
    assert row.schema_id is None
    assert row.schema_revision_id is None
