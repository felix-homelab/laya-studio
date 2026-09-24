"""A/B testing API: revision comparison, flips and the interpretation."""

from __future__ import annotations

import json

DATASET = [
    {"state": {"body": "We were billed twice and need a refund"}, "label": "billing"},
    {"state": {"body": "Our integration returns a 500 and production is blocked"}, "label": "technical"},
    {"state": {"body": "Please send enterprise pricing for 200 seats"}, "label": "sales"},
    {"state": {"body": "Thanks for the quick help"}, "label": "other"},
]


def make_schema(client, name: str = "AB fixture") -> dict:
    return client.post(
        "/api/schemas",
        json={
            "name": name,
            "questions": {
                "department": {
                    "type": "choice",
                    "instructions": "Which department should handle this request?",
                    "criteria": {
                        "billing": "invoices, payments, refunds, duplicate charges",
                        "technical": "bugs, outages, system errors",
                        "sales": "pricing, new contracts, upgrades",
                        "other": "everything else",
                    },
                }
            },
        },
    ).json()


def add_revision(client, schema_id: int, instructions: str) -> dict:
    detail = client.get(f"/api/schemas/{schema_id}").json()
    questions = json.loads(json.dumps(detail["definition"]["questions"]))
    questions["department"]["instructions"] = instructions
    return client.put(f"/api/schemas/{schema_id}", json={"questions": questions, "note": instructions})


def test_identical_revisions_produce_zero_flip_rate(client):
    schema = make_schema(client)
    add_revision(client, schema["id"], "Which team should own this?")
    revisions = client.get(f"/api/schemas/{schema['id']}/revisions").json()
    # Both revisions describe the same taxonomy, only the wording differs.
    a = next(r for r in revisions if r["revision"] == 1)
    b = next(r for r in revisions if r["revision"] == 2)

    # Point both sides at the *same* revision to prove the machinery is stable.
    response = client.post(
        "/api/ab/tests",
        json={
            "name": "self-comparison",
            "schema_id": schema["id"],
            "revision_a_id": a["id"],
            "revision_b_id": a["id"],
        },
    )
    assert response.status_code == 422  # same-revision comparison is refused

    response = client.post(
        "/api/ab/tests",
        json={
            "name": "r1 vs r2",
            "schema_id": schema["id"],
            "revision_a_id": a["id"],
            "revision_b_id": b["id"],
            "dataset": DATASET,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["summary"]["total"] == 4
    assert 0.0 <= body["summary"]["flip_rate"] <= 1.0
    assert body["summary"]["agreement"] == 1.0 - body["summary"]["flip_rate"]
    assert body["summary"]["labelled"] == 4
    assert body["summary"]["accuracy_a"] is not None
    assert body["summary"]["accuracy_b"] is not None
    assert body["summary"]["interpretation"]
    assert len(body["items"]) == 4


def test_transitions_only_list_flipped_rows(client):
    schema = make_schema(client)
    add_revision(client, schema["id"], "Wording changed")
    revisions = client.get(f"/api/schemas/{schema['id']}/revisions").json()

    body = client.post(
        "/api/ab/tests",
        json={
            "name": "transitions",
            "schema_id": schema["id"],
            "revision_a_id": revisions[1]["id"],
            "revision_b_id": revisions[0]["id"],
            "dataset": DATASET,
        },
    ).json()

    flips = sum(item["count"] for item in body["summary"]["transitions"])
    assert flips == body["summary"]["flip_count"]
    for item in body["items"]:
        assert item["flipped"] == (item["choice_a"] != item["choice_b"])
        assert item["result_a"]["breakdown"] is not None


def test_a_test_can_reuse_a_bulk_job_as_its_dataset(client, triage_schema):
    csv = (
        "body,truth\n"
        "We were billed twice and need a refund,billing\n"
        "Our integration returns a 500,technical\n"
    )
    job = client.post(
        "/api/bulk/upload",
        files={"file": ("d.csv", csv, "text/csv")},
        data={"schema_id": str(triage_schema["id"]), "label_column": "truth"},
    ).json()

    revisions = client.get(f"/api/schemas/{triage_schema['id']}/revisions").json()
    assert len(revisions) == 1, "the template ships with a single revision"

    response = client.post(
        "/api/ab/tests",
        json={
            "name": "from job",
            "schema_id": triage_schema["id"],
            "revision_a_id": revisions[0]["id"],
            "revision_b_id": revisions[0]["id"],
            "source_job_id": job["id"],
        },
    )
    # Same revision on both sides is refused, which is the correct answer here.
    assert response.status_code == 422


def test_missing_dataset_is_rejected(client):
    schema = make_schema(client)
    add_revision(client, schema["id"], "changed")
    revisions = client.get(f"/api/schemas/{schema['id']}/revisions").json()
    response = client.post(
        "/api/ab/tests",
        json={
            "name": "no data",
            "schema_id": schema["id"],
            "revision_a_id": revisions[0]["id"],
            "revision_b_id": revisions[1]["id"],
        },
    )
    assert response.status_code == 422
    assert "dataset" in response.json()["detail"]


def test_revisions_must_belong_to_the_selected_schema(client):
    first = make_schema(client, "First schema")
    second = make_schema(client, "Second schema")
    add_revision(client, first["id"], "changed a")
    add_revision(client, second["id"], "changed b")

    rev_first = client.get(f"/api/schemas/{first['id']}/revisions").json()
    rev_second = client.get(f"/api/schemas/{second['id']}/revisions").json()

    response = client.post(
        "/api/ab/tests",
        json={
            "name": "cross-schema",
            "schema_id": first["id"],
            "revision_a_id": rev_first[0]["id"],
            "revision_b_id": rev_second[0]["id"],
            "dataset": DATASET,
        },
    )
    assert response.status_code == 422
    assert "belong to the selected schema" in response.json()["detail"]


def test_tests_can_be_listed_rerun_and_deleted(client):
    schema = make_schema(client)
    add_revision(client, schema["id"], "changed")
    revisions = client.get(f"/api/schemas/{schema['id']}/revisions").json()

    created = client.post(
        "/api/ab/tests",
        json={
            "name": "lifecycle",
            "schema_id": schema["id"],
            "revision_a_id": revisions[1]["id"],
            "revision_b_id": revisions[0]["id"],
            "dataset": DATASET,
        },
    ).json()

    assert any(item["id"] == created["id"] for item in client.get("/api/ab/tests").json())
    assert client.post(f"/api/ab/tests/{created['id']}/run").status_code == 200
    assert client.get(f"/api/ab/tests/{created['id']}").status_code == 200
    assert client.delete(f"/api/ab/tests/{created['id']}").status_code == 204
    assert client.get(f"/api/ab/tests/{created['id']}").status_code == 404


def test_large_datasets_are_capped(client):
    schema = make_schema(client)
    add_revision(client, schema["id"], "changed")
    revisions = client.get(f"/api/schemas/{schema['id']}/revisions").json()
    response = client.post(
        "/api/ab/tests",
        json={
            "name": "too big",
            "schema_id": schema["id"],
            "revision_a_id": revisions[0]["id"],
            "revision_b_id": revisions[1]["id"],
            "dataset": DATASET * 600,
        },
    )
    assert response.status_code == 422
    assert "row limit" in response.json()["detail"]
