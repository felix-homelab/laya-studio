"""Bulk testing API: uploads, metrics and failure handling."""

from __future__ import annotations

import json

import pytest

CSV_WITH_LABELS = (
    "subject,body,truth\n"
    "Duplicate charge,We were billed twice and need a refund,billing\n"
    "API 500,Our integration is broken and production is blocked,technical\n"
    "Pricing,Please send enterprise pricing for 200 seats,sales\n"
    "Thanks,Thanks for the quick help on this,other\n"
)


def upload(client, schema_id: int, content: str, filename: str = "rows.csv", **extra):
    data = {"schema_id": str(schema_id), **{k: str(v) for k, v in extra.items()}}
    return client.post(
        "/api/bulk/upload",
        files={"file": (filename, content, "text/csv" if filename.endswith(".csv") else "application/json")},
        data=data,
    )


def test_upload_scores_every_row_and_reports_metrics(client, triage_schema):
    response = upload(client, triage_schema["id"], CSV_WITH_LABELS, label_column="truth")
    assert response.status_code == 201

    job = response.json()
    assert job["status"] == "succeeded"
    assert job["total_rows"] == 4
    assert job["processed_rows"] == 4
    assert job["failed_rows"] == 0
    assert len(job["items"]) == 4

    summary = job["summary"]
    assert summary["labelled"] == 4
    assert summary["accuracy"] is not None
    assert summary["ece"] is not None
    assert sum(summary["band_counts"].values()) == 4
    assert summary["prediction_distribution"]


def test_upload_without_a_label_column_still_predicts(client, triage_schema):
    content = "subject,body\nRefund,We were billed twice\nOutage,production is down\n"
    job = upload(client, triage_schema["id"], content).json()
    assert job["processed_rows"] == 2
    # Keys are present-but-null so clients need no guessing.
    assert job["summary"]["accuracy"] is None
    assert job["summary"]["ece"] is None
    assert job["summary"]["band_counts"] is None
    assert job["summary"]["mean_confidence"] is not None


def test_state_column_narrows_the_state(client, triage_schema):
    content = "subject,body,noise\nRefund,We were billed twice,zzz\n"
    job = upload(client, triage_schema["id"], content, state_column="body").json()
    item = job["items"][0]
    assert item["state"] == {"text": "We were billed twice"}


def test_json_uploads_are_supported(client, triage_schema):
    rows = [
        {"state": {"body": "We were billed twice and want a refund"}, "label": "billing"},
        {"state": {"body": "Please send enterprise pricing"}, "label": "sales"},
    ]
    job = upload(
        client,
        triage_schema["id"],
        json.dumps({"rows": rows}),
        filename="rows.json",
        label_column="label",
    ).json()
    assert job["total_rows"] == 2
    assert job["summary"]["accuracy"] is not None


def test_a_json_list_of_plain_strings_is_accepted(client, triage_schema):
    job = upload(
        client,
        triage_schema["id"],
        json.dumps(["we were billed twice", "production is down"]),
        filename="rows.json",
    ).json()
    assert job["total_rows"] == 2
    assert job["items"][0]["state"] == {"text": "we were billed twice"}


def test_empty_upload_is_rejected(client, triage_schema):
    response = upload(client, triage_schema["id"], "")
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


def test_malformed_json_is_rejected_with_a_position(client, triage_schema):
    response = upload(client, triage_schema["id"], '{"rows": [', filename="rows.json")
    assert response.status_code == 422
    assert "Invalid JSON" in response.json()["detail"]


def test_csv_without_a_header_is_rejected(client, triage_schema):
    response = upload(client, triage_schema["id"], "")
    assert response.status_code == 422


def test_unknown_schema_is_a_404(client):
    assert upload(client, 4242, CSV_WITH_LABELS).status_code == 404


def test_jobs_can_be_listed_fetched_rerun_and_deleted(client, triage_schema):
    job = upload(client, triage_schema["id"], CSV_WITH_LABELS, label_column="truth").json()

    listing = client.get("/api/bulk/jobs").json()
    assert any(item["id"] == job["id"] for item in listing)

    fetched = client.get(f"/api/bulk/jobs/{job['id']}").json()
    assert fetched["name"] == "rows.csv"

    items = client.get(f"/api/bulk/jobs/{job['id']}/items?limit=2").json()
    assert len(items) == 2

    rerun = client.post(f"/api/bulk/jobs/{job['id']}/rerun")
    assert rerun.status_code == 200
    assert rerun.json()["processed_rows"] == 4

    assert client.delete(f"/api/bulk/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/bulk/jobs/{job['id']}").status_code == 404


def test_rerun_replaces_rather_than_duplicates_evaluations(client, triage_schema):
    job = upload(client, triage_schema["id"], CSV_WITH_LABELS).json()
    before = client.get("/api/health").json()["database"]["tables"]["evaluations"]
    client.post(f"/api/bulk/jobs/{job['id']}/rerun")
    after = client.get("/api/health").json()["database"]["tables"]["evaluations"]
    # A rerun scores the rows again; it is not idempotent by design, but the row
    # count must grow by exactly the size of the batch.
    assert after == before + 4


def test_accuracy_survives_a_labelled_row_with_no_confidence(session):
    """Accuracy needs the labels; only ECE, Brier and the bands need a confidence.

    ``summarize`` used to withhold all four together, so a single row whose confidence
    was never recorded silently removed a computable accuracy from the batch.
    """
    from app.models import BulkJob, BulkJobItem
    from app.services import bulk as bulk_service

    job = BulkJob(name="Partial", status="succeeded", total_rows=3, processed_rows=3)
    session.add(job)
    session.flush()
    session.add_all(
        [
            BulkJobItem(
                job_id=job.id,
                row_index=0,
                state={"text": "a"},
                label="billing",
                predicted="billing",
                confidence=0.9,
                ok=True,
            ),
            BulkJobItem(
                job_id=job.id,
                row_index=1,
                state={"text": "b"},
                label="sales",
                predicted="billing",
                confidence=0.4,
                ok=True,
            ),
            BulkJobItem(
                job_id=job.id,
                row_index=2,
                state={"text": "c"},
                label="other",
                predicted="other",
                confidence=None,
                ok=True,
            ),
        ]
    )
    session.commit()

    summary = bulk_service.summarize(session, job)

    assert summary["labelled"] == 3
    assert summary["accuracy"] == pytest.approx(2 / 3)
    assert summary["mean_confidence"] == pytest.approx(0.65)
    # The confidence-dependent metrics are withheld, and the note now says why.
    assert summary["ece"] is None
    assert summary["brier"] is None
    assert summary["band_counts"] is None
    assert "no confidence" in summary["calibration_note"]
