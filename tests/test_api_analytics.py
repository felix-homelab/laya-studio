"""Analytics API: overview metrics, time series, and the calibration monitor."""

from __future__ import annotations

import pytest


def seed_evaluations(client, triage_schema, count: int = 12, *, label: bool = True) -> None:
    bodies = [
        "We were billed twice and need a refund immediately.",
        "Our integration returns a 500 and production is blocked.",
        "Please send enterprise pricing for 200 seats.",
        "Thanks for the quick help, everything works now.",
    ]
    truths = ["billing", "technical", "sales", "other"]
    for index in range(count):
        created = client.post(
            "/api/playground/evaluate",
            json={
                "state": {"body": bodies[index % len(bodies)]},
                "schema_id": triage_schema["id"],
                "primary_question": "department",
            },
        ).json()
        if label:
            client.post(
                f"/api/playground/evaluations/{created['id']}/label",
                json={"label": truths[index % len(truths)]},
            )


def test_health_reports_the_engine_and_row_counts(client, triage_schema):
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["engine"]["state"] == "ready"
    assert health["engine"]["ready"] is True
    assert health["engine"]["name"] == "lexical"
    assert health["llm"]["enabled"] is False
    assert health["thresholds"] == {"auto": 0.90, "verify": 0.60}
    assert health["database"]["tables"]["schemas"] == 4


def test_overview_is_empty_before_anything_runs(client):
    overview = client.get("/api/analytics/overview").json()
    assert overview["total"] == 0
    assert "No evaluations" in overview["note"]


def test_overview_reports_bands_and_rates(client, triage_schema):
    seed_evaluations(client, triage_schema, 12)
    overview = client.get("/api/analytics/overview").json()

    assert overview["total"] == 12
    bands = overview["bands"]
    assert sum(bands.values()) == 12
    assert overview["auto_rate"] + overview["verify_rate"] + overview["escalation_rate"] == pytest.approx(1.0)
    assert overview["confidence"]["mean"] is not None
    assert overview["latency_ms"]["mean"] is not None
    assert overview["by_source"]["playground"] == 12
    assert overview["top_choices"]


def test_overview_reports_calibration_only_with_labels(client, triage_schema):
    seed_evaluations(client, triage_schema, 8, label=False)
    overview = client.get("/api/analytics/overview").json()
    assert overview["labelled"] == 0
    assert overview["accuracy"] is None
    assert overview["ece"] is None


def test_calibration_report_needs_labels_then_computes_them(client, triage_schema):
    seed_evaluations(client, triage_schema, 4, label=False)
    empty = client.get("/api/analytics/calibration").json()
    assert empty["n_samples"] == 0
    assert "No labelled rows" in empty["note"]

    seed_evaluations(client, triage_schema, 8, label=True)
    report = client.get("/api/analytics/calibration").json()
    assert report["n_samples"] == 8
    assert report["ece"] is not None
    assert report["brier"] is not None
    assert len(report["bins"]) == 10
    assert report["interpretation"]
    assert report["trend"]


def test_histogram_bins_are_flagged_with_their_band(client, triage_schema):
    seed_evaluations(client, triage_schema, 8)
    histogram = client.get("/api/analytics/dashboard").json()["histogram"]
    assert len(histogram) == 10
    assert all(item["band"] in ("auto", "verify", "review") for item in histogram)
    assert sum(item["count"] for item in histogram) == 8


def test_timeseries_buckets_by_hour_and_day(client, triage_schema):
    seed_evaluations(client, triage_schema, 6)

    hourly = client.get("/api/analytics/timeseries?hours=24&bucket=hour").json()
    assert hourly
    assert hourly[0]["count"] >= 1
    assert "escalation_rate" in hourly[0]

    daily = client.get("/api/analytics/timeseries?hours=168&bucket=day").json()
    assert len(daily) <= len(hourly)

    assert client.get("/api/analytics/timeseries?bucket=fortnight").status_code == 422


def test_route_breakdown_counts_workflow_runs(client, triage_schema):
    workflow = client.post(
        "/api/workflows",
        json={
            "name": "Stats",
            "schema_id": triage_schema["id"],
            "decision_question": "department",
            "steps": [{"on": "review", "kind": "review", "config": {}}],
        },
    ).json()
    client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "zzz qqq"})

    routes = client.get("/api/analytics/routes").json()
    assert routes["runs"] == 1
    assert sum(routes["by_route"].values()) == 1
    assert routes["review_items"] == 1
    assert routes["review_open"] == 1


def test_dashboard_returns_every_section_in_one_call(client, triage_schema):
    seed_evaluations(client, triage_schema, 6)
    body = client.get("/api/analytics/dashboard?hours=168&bucket=day").json()
    assert set(body) == {"overview", "histogram", "timeseries", "routes", "calibration", "thresholds"}
    assert body["thresholds"] == {"auto": 0.90, "verify": 0.60}


def test_refit_reports_nothing_to_do_without_labels(client):
    response = client.post("/api/analytics/calibration/refit")
    assert response.status_code == 200
    assert response.json()["snapshots"] == []
    assert "Nothing to refit" in response.json()["message"]


def test_refit_produces_snapshots_with_enough_labelled_rows(client, triage_schema):
    seed_evaluations(client, triage_schema, 24)
    response = client.post("/api/analytics/calibration/refit")
    assert response.status_code == 200
    snapshots = response.json()["snapshots"]
    assert snapshots
    first = snapshots[0]
    assert first["question"] == "department"
    assert first["temperature"] > 0
    assert first["n_samples"] >= 10

    listed = client.get("/api/analytics/calibration").json()["snapshots"]
    assert listed


def test_calibration_report_slice(client, triage_schema):
    seed_evaluations(client, triage_schema, 6)
    report = client.get(
        f"/api/analytics/calibration/report?schema_id={triage_schema['id']}&question=department"
    ).json()
    assert report["n_samples"] == 6

    empty = client.get("/api/analytics/calibration/report?question=nonexistent").json()
    assert empty["n_samples"] == 0


def test_thresholds_endpoint_exposes_the_live_ladder(client):
    body = client.get("/api/analytics/thresholds").json()
    assert body["auto"] == 0.90
    assert body["verify"] == 0.60
    assert body["calibration_enabled"] is True
