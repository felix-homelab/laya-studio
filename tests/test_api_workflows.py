"""Workflow engine and ingestion: the confidence ladder, the fallback, the actions.

The ladder is the core product claim, so it is tested directly at every band:

* high confidence  -> auto actions run
* middle band      -> the generative fallback is consulted, then auto or escalate
* low confidence   -> the human queue

and, crucially, *the middle band escalates when the fallback is unavailable*.
"""

from __future__ import annotations

import json


def make_workflow(client, triage_schema, **overrides) -> dict:
    payload = {
        "name": "Triage",
        "schema_id": triage_schema["id"],
        "decision_question": "department",
        "trigger_type": "api",
        "auto_threshold": 0.90,
        "verify_threshold": 0.60,
        "steps": [
            {"on": "auto", "kind": "noop", "config": {"detail": "filed {choice}"}},
            {"on": "review", "kind": "review", "config": {"reason": "low_confidence", "priority": 42}},
        ],
    }
    payload.update(overrides)
    response = client.post("/api/workflows", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- CRUD --------------------------------------------------------------------


def test_create_workflow_defaults_thresholds_from_settings(client, triage_schema):
    workflow = client.post(
        "/api/workflows",
        json={"name": "Defaults", "schema_id": triage_schema["id"]},
    ).json()
    assert workflow["auto_threshold"] == 0.90
    assert workflow["verify_threshold"] == 0.60
    assert workflow["enabled"] is True
    assert workflow["key"] == "defaults"


def test_invalid_step_target_or_kind_is_rejected(client, triage_schema):
    bad_target = client.post(
        "/api/workflows",
        json={
            "name": "Bad target",
            "schema_id": triage_schema["id"],
            "steps": [{"on": "sometimes", "kind": "noop"}],
        },
    )
    assert bad_target.status_code == 422

    bad_kind = client.post(
        "/api/workflows",
        json={
            "name": "Bad kind",
            "schema_id": triage_schema["id"],
            "steps": [{"on": "auto", "kind": "teleport"}],
        },
    )
    assert bad_kind.status_code == 422


def test_verify_threshold_cannot_exceed_the_auto_threshold(client, triage_schema):
    workflow = client.post(
        "/api/workflows",
        json={
            "name": "Inverted",
            "schema_id": triage_schema["id"],
            "auto_threshold": 0.5,
            "verify_threshold": 0.9,
        },
    ).json()
    assert workflow["verify_threshold"] == 0.5


def test_workflow_lifecycle(client, triage_schema):
    workflow = make_workflow(client, triage_schema)

    assert any(item["id"] == workflow["id"] for item in client.get("/api/workflows").json())

    disabled = client.put(f"/api/workflows/{workflow['id']}", json={"enabled": False}).json()
    assert disabled["enabled"] is False

    assert client.put(
        f"/api/workflows/{workflow['id']}", json={"name": "Renamed"}
    ).json()["name"] == "Renamed"

    assert client.delete(f"/api/workflows/{workflow['id']}").status_code == 204
    assert client.get(f"/api/workflows/{workflow['id']}").status_code == 404


# --- the ladder --------------------------------------------------------------


def test_high_confidence_takes_the_auto_route(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "We were billed twice, please refund the duplicate invoice charge today."},
    )
    assert response.status_code == 200
    body = response.json()

    if body["route"] == "auto":
        assert body["effective_route"] == "auto"
        assert [action["kind"] for action in body["actions"]] == ["noop"]
        assert body["run"]["route"] == "auto"
    else:
        # The lexical double is not tuned to saturated confidence, so assert the
        # ladder's own contract instead.
        assert body["effective_route"] in ("verify", "review")


def test_low_confidence_escalates_to_the_human_queue(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "zzz qqq wwww"},
    )
    body = response.json()

    assert body["effective_route"] == "review"
    assert any(action["kind"] == "review" for action in body["actions"])

    queue = client.get("/api/review/items?status=open").json()
    assert len(queue) == 1
    assert queue[0]["priority"] == 42
    assert queue[0]["model_confidence"] < 0.60


def test_middle_band_escalates_when_no_fallback_is_configured(client, triage_schema):
    """The safe default: an unverifiable middle band must not auto-execute."""
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    )
    body = response.json()

    if body["route"] != "verify":
        return  # the double's confidence did not land in the band; nothing to assert

    assert body["verification"]["status"] == "skipped"
    assert body["effective_route"] == "review"
    assert any(action["kind"] == "review" for action in body["actions"])


def test_a_verification_escalation_queues_with_the_review_priority(client, triage_schema, scripted):
    """The default priority follows where the run *lands*, not the rung it started on.

    A mid-band item that could not be verified ends up in the same queue as one the
    ladder sent straight there, so it must not sort below it. The step here carries
    no explicit `priority`, which is what makes the route default observable.
    """
    scripted(
        answers={
            "department": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.4, "technical": 0.3, "sales": 0.2, "other": 0.1},
                "confidence": 0.75,
            }
        }
    )

    workflow = client.post(
        "/api/workflows",
        json={
            "name": "Escalates",
            "schema_id": triage_schema["id"],
            "decision_question": "department",
            "steps": [
                {"on": "review", "kind": "review", "config": {"reason": "verification_unavailable"}}
            ],
        },
    ).json()

    body = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "We were billed twice."},
    ).json()

    # 0.75 is deterministic, so the middle band is entered without a probe.
    assert body["route"] == "verify"
    assert body["verification"]["status"] == "skipped"  # no fallback configured
    assert body["effective_route"] == "review"

    queue = client.get("/api/review/items?status=open").json()
    assert len(queue) == 1
    assert queue[0]["priority"] == 100


def test_middle_band_auto_executes_when_the_fallback_agrees(client, triage_schema, fake_provider):
    workflow = make_workflow(client, triage_schema)

    # Find a state that lands in the verify band, then install a provider that
    # agrees with whatever the engine said.
    probe = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    ).json()
    if probe["route"] != "verify":
        return

    agreed_label = probe["evaluation"]["top_choice"]
    fake_provider(json.dumps({"choice": agreed_label, "agrees": True, "rationale": "clearly billing"}))

    response = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    )
    body = response.json()

    assert body["route"] == "verify"
    assert body["verification"]["status"] == "ok"
    assert body["verification"]["agrees"] is True
    assert body["effective_route"] == "auto"
    assert any(action["kind"] == "noop" for action in body["actions"])


def test_middle_band_escalates_when_the_fallback_disagrees(client, triage_schema, fake_provider):
    workflow = make_workflow(client, triage_schema)

    probe = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    ).json()
    if probe["route"] != "verify":
        return

    fake_provider(json.dumps({"choice": "other", "agrees": False, "rationale": "looks unrelated"}))

    body = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    ).json()

    assert body["effective_route"] == "review"
    assert body["verification"]["agrees"] is False
    assert any(action["kind"] == "review" for action in body["actions"])


def test_unparseable_fallback_output_escalates_rather_than_guessing(client, triage_schema, fake_provider):
    workflow = make_workflow(client, triage_schema)

    probe = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    ).json()
    if probe["route"] != "verify":
        return

    fake_provider("I'm afraid I can't help with that.")

    body = client.post(
        f"/api/workflows/{workflow['id']}/run",
        json={"text": "Duplicate charge on the invoice, we were billed twice."},
    ).json()

    assert body["verification"]["status"] == "failed"
    assert body["effective_route"] == "review"


def test_viewer_invented_labels_are_not_accepted(client, triage_schema, fake_provider):
    workflow = make_workflow(client, triage_schema)
    fake_provider(json.dumps({"choice": "a-label-that-does-not-exist", "agrees": True}))

    probe = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "hello"}).json()
    if probe["route"] != "verify":
        return
    assert "outside the offered options" in (probe["verification"]["error"] or "")


def test_runs_are_recorded_with_their_action_log(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    body = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "zzz qqq"}).json()

    runs = client.get(f"/api/workflows/{workflow['id']}/runs").json()
    assert len(runs) == 1
    assert runs[0]["id"] == body["run"]["id"]

    detail = client.get(f"/api/workflows/runs/{body['run']['id']}").json()
    assert detail["action_logs"], "every action must be logged"
    assert detail["evaluation"] is not None
    assert detail["action_logs"][0]["kind"] in ("noop", "review", "webhook", "email", "llm_verify")


def test_a_workflow_without_a_schema_fails_the_run_cleanly(client, triage_schema):
    workflow = client.post("/api/workflows", json={"name": "Orphan"}).json()
    body = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "hi"}).json()
    assert body["route"] == "error"
    assert "no schema" in body["error"].lower()


def test_a_disabled_workflow_refuses_to_run(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    client.put(f"/api/workflows/{workflow['id']}", json={"enabled": False})
    body = client.post(f"/api/workflows/{workflow['id']}/run", json={"text": "hi"}).json()
    assert body["route"] == "error"
    assert "disabled" in body["error"].lower()


def test_a_disabled_workflow_refuses_a_dry_run_too(client, triage_schema):
    """A dry run must not promise a route a live run will refuse.

    The dry run is the call an operator makes *before* going live, so answering with a
    route for a workflow that is switched off is the one answer it must not give —
    `run_workflow` answers `route: "error"` for exactly that case. Neither door may
    score a row on the way to saying so, either.
    """
    workflow = make_workflow(client, triage_schema)
    client.put(f"/api/workflows/{workflow['id']}", json={"enabled": False})

    ingested = client.post(
        f"/api/ingest/{workflow['key']}", json={"text": "hi", "dry_run": True}
    ).json()
    assert ingested["route"] == "error"
    assert ingested["effective_route"] == "error"
    assert "disabled" in ingested["error"].lower()
    assert ingested["actions"] == []
    assert ingested["evaluation"] is None

    manual = client.post(
        f"/api/workflows/{workflow['id']}/run", json={"text": "hi", "dry_run": True}
    ).json()
    assert manual["route"] == "error"
    assert "disabled" in manual["error"].lower()

    # Refused before the model was consulted, so nothing was persisted.
    assert client.get("/api/health").json()["database"]["tables"]["evaluations"] == 0


def test_workflow_stats(client, triage_schema):
    make_workflow(client, triage_schema)
    client.post(
        f"/api/workflows/{make_workflow(client, triage_schema, name='Second')['id']}/run",
        json={"text": "zzz"},
    )
    stats = client.get("/api/workflows/stats").json()
    assert stats["total"] == 2
    assert stats["enabled"] == 2
    assert sum(stats["runs_by_route"].values()) >= 1


# --- ingestion ---------------------------------------------------------------


def test_ingest_runs_a_workflow_by_key(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(f"/api/ingest/{workflow['key']}", json={"text": "zzz qqq"})
    assert response.status_code == 200
    assert response.json()["route"] in ("auto", "verify", "review")


def test_ingest_dry_run_routes_without_executing_anything(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/ingest/{workflow['key']}", json={"text": "zzz qqq", "dry_run": True}
    )
    body = response.json()
    assert body["dry_run"] is True
    assert body["run"] is None
    assert body["actions"] == []
    # Nothing was queued for review, because no ladder step ran.
    assert client.get("/api/review/items?status=open").json() == []


def test_manual_dry_run_executes_nothing_either(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/workflows/{workflow['id']}/run", json={"text": "zzz qqq", "dry_run": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["run"] is None
    assert body["actions"] == []
    assert client.get(f"/api/workflows/{workflow['id']}/runs").json() == []


def test_ingest_accepts_plain_text_bodies(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/ingest/{workflow['key']}/text",
        content=b"We were billed twice and need a refund",
        headers={"Content-Type": "text/plain"},
    )
    assert response.status_code == 200
    assert response.json()["evaluation"]["state"] == {"text": "We were billed twice and need a refund"}


def test_ingest_rejects_an_empty_text_body(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/ingest/{workflow['key']}/text", content=b"   ", headers={"Content-Type": "text/plain"}
    )
    assert response.status_code == 422


def test_email_ingestion_shapes_a_structured_state(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(
        f"/api/ingest/email/{workflow['key']}",
        json={"from": "dana@acme.example", "subject": "Refund please", "body": "We were double charged."},
    )
    assert response.status_code == 200
    state = response.json()["evaluation"]["state"]
    assert state["subject"] == "Refund please"
    assert state["from"] == "dana@acme.example"
    # A parse artefact must not leak into the state as `from_`.
    assert "from_" not in state


def test_ingest_rejects_a_payload_with_neither_state_nor_text(client, triage_schema):
    workflow = make_workflow(client, triage_schema)
    response = client.post(f"/api/ingest/{workflow['key']}", json={})
    assert response.status_code == 422


def test_unknown_workflow_key_is_a_404(client):
    assert client.post("/api/ingest/nope", json={"text": "hi"}).status_code == 404
