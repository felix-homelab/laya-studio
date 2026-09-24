"""The MCP endpoint: handshake, tool surface, and the tools doing real work.

These go over HTTP through the mounted endpoint rather than calling the SDK in
process, because that is the contract that matters: a client POSTs JSON-RPC to
``/mcp`` and gets an answer. Two details of the setup are deliberate:

* The test client uses ``http://127.0.0.1:8000`` as its base URL, not the default
  ``http://testserver``. The endpoint keeps DNS-rebinding protection on, so a Host
  header the panel has not been told about is refused with a 421 — proven by
  ``test_requests_from_an_unexpected_host_are_refused``.
* Calls go through the *real* app, so the tools execute against the test database
  and the engine double like any other request.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import reload_settings
from app.observability import get_store

#: What an MCP client sends first. The protocol version is the one clients
#: negotiate today; the endpoint answers with its own.
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "laya-studio-tests", "version": "0"},
    },
}

JSON_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}

#: A Host header the endpoint is configured to accept.
PANEL_ORIGIN = "http://127.0.0.1:8000"


@contextmanager
def environment(**values: str) -> Iterator[None]:
    """Patch the environment and rebuild settings, restoring both afterwards.

    The token and enable/disable tests need a *fresh app* built from a patched
    environment, and settings are cached, so the cache has to be dropped on both
    sides of the patch.
    """
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    reload_settings()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reload_settings()


@pytest.fixture
def mcp_client(fresh_db):
    """A test client whose Host header looks like a real client's."""
    from app.main import app

    with TestClient(app, base_url=PANEL_ORIGIN) as client:
        yield client


def rpc(client: TestClient, method: str, params: dict | None = None, request_id: int = 2):
    payload: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return client.post("/mcp", json=payload, headers=JSON_HEADERS)


def call_tool(client: TestClient, name: str, arguments: dict | None = None) -> dict:
    """Call one tool and return the decoded JSON of its content block."""
    response = rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body["error"]
    text = body["result"]["content"][0]["text"]
    return {"is_error": body["result"].get("isError", False), "data": text}


# --- transport ---------------------------------------------------------------


def test_handshake_lists_the_panel_and_its_tools(mcp_client):
    response = mcp_client.post("/mcp", json=INITIALIZE, headers=JSON_HEADERS)
    assert response.status_code == 200, response.text
    result = response.json()["result"]

    assert result["serverInfo"]["name"] == "laya-studio"
    assert result["capabilities"]["tools"]
    # The instructions are what an agent reads before touching a tool: they have to
    # say what this panel is, and warn about the destructive surface.
    assert "Laya" in result["instructions"]
    assert "health" in result["instructions"]


def test_the_endpoint_answers_statusfully_without_a_session(mcp_client):
    """Stateless transport: every POST stands alone, no session id echoed back.

    This is the property that keeps a mounted endpoint usable — a client that
    reconnects, restarts, or simply does not echo the header still works.
    """
    first = mcp_client.post("/mcp", json=INITIALIZE, headers=JSON_HEADERS)
    assert first.status_code == 200
    assert first.headers.get("mcp-session-id") is None

    assert rpc(mcp_client, "tools/list").status_code == 200


def test_the_path_is_exact_and_not_a_redirect(mcp_client):
    """``/mcp`` itself must answer, not 307 to ``/mcp/``."""
    response = mcp_client.post("/mcp", json=INITIALIZE, headers=JSON_HEADERS)
    assert response.status_code == 200


def test_requests_from_an_unexpected_host_are_refused(fresh_db):
    """DNS-rebinding protection: the endpoint is not open to any Host header."""
    from app.main import app

    with TestClient(app, base_url="http://an-attacker.example") as client:
        response = client.post("/mcp", json=INITIALIZE, headers=JSON_HEADERS)
        assert response.status_code == 421


# --- the tool surface --------------------------------------------------------


def test_every_area_of_the_panel_has_tools(mcp_client):
    response = rpc(mcp_client, "tools/list")
    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}

    expected = {
        # observability
        "logs_search", "logs_tail", "log_stats", "errors_list", "errors_detail",
        "request_log", "db_query",
        # the decision surface
        "health", "readiness", "evaluate", "route", "lint_questions",
        # the modules
        "schemas_list", "schema_create", "schema_fork", "schema_delete",
        "bulk_upload", "bulk_job_get", "bulk_job_rerun",
        "ab_test_create", "ab_test_run",
        "workflow_create", "workflow_run", "workflow_runs", "workflow_run_get",
        "review_items", "review_resolve", "review_retry",
        "analytics_overview", "analytics_calibration", "calibration_refit",
        "ingest", "ingest_email",
    }
    assert expected <= set(tools)

    # 60-odd tools is a lot to hand an agent, so the annotations have to be honest
    # about which ones change state.
    assert tools["evaluate"]["annotations"]["readOnlyHint"] is False
    assert tools["logs_search"]["annotations"]["readOnlyHint"] is True
    assert tools["schema_delete"]["annotations"]["destructiveHint"] is True
    assert tools["workflow_run"]["annotations"]["destructiveHint"] is True
    # Descriptions carry the semantics an agent cannot guess (the ladder, Laya's
    # entropic confidence, dry runs).
    assert "dry_run" in tools["workflow_run"]["description"]
    assert "entropy" in tools["evaluate"]["description"]


def test_tools_have_usable_input_schemas(mcp_client):
    tools = {tool["name"]: tool for tool in rpc(mcp_client, "tools/list").json()["result"]["tools"]}

    evaluate = tools["evaluate"]["inputSchema"]
    assert evaluate["required"] == ["state"]
    assert set(evaluate["properties"]) >= {"state", "schema_key", "questions", "persist"}

    logs = tools["logs_search"]["inputSchema"]["properties"]
    assert {"level", "kind", "q", "since", "include_traceback"} <= set(logs)


# --- the tools themselves ----------------------------------------------------


def test_list_schemas_returns_the_builtin_templates(mcp_client):
    result = call_tool(mcp_client, "schemas_list")
    payload = json.loads(result["data"])

    assert result["is_error"] is False
    # A collection comes back as one object, `{count, items}`, never as a bare array.
    assert payload["count"] == len(payload["items"]) == 4
    assert any(item["key"] == "email-triage" for item in payload["items"])
    assert all(item["is_template"] for item in payload["items"])


def test_evaluate_scores_a_state_with_the_engine(mcp_client):
    schemas = json.loads(call_tool(mcp_client, "schemas_list")["data"])["items"]
    schema_id = next(item["id"] for item in schemas if item["key"] == "email-triage")

    result = call_tool(
        mcp_client,
        "evaluate",
        {
            "state": "We were billed twice for March. Please refund the duplicate.",
            "schema_id": schema_id,
            "persist": True,
        },
    )
    evaluation = json.loads(result["data"])

    assert result["is_error"] is False
    assert evaluation["id"] is not None
    assert evaluation["question_results"]
    assert evaluation["engine"] == "lexical"  # the suite's double
    assert evaluation["routing"]["model"] == "english"


def test_a_mutation_through_a_tool_lands_in_the_database(mcp_client):
    created = json.loads(
        call_tool(
            mcp_client,
            "schema_create",
            {
                "name": "MCP-made schema",
                "questions": {
                    "topic": {
                        "type": "choice",
                        "instructions": "Pick one.",
                        "criteria": {"billing": "money", "other": "anything else"},
                    }
                },
            },
        )["data"]
    )
    assert created["key"] == "mcp-made-schema"

    listed = json.loads(call_tool(mcp_client, "schemas_list", {"search": "MCP-made"})["data"])
    assert listed["count"] == 1
    assert [item["id"] for item in listed["items"]] == [created["id"]]


def test_api_errors_come_back_as_tool_errors_not_crashes(mcp_client):
    missing = call_tool(mcp_client, "schema_get", {"schema_id": 999999})
    assert missing["is_error"] is True
    assert "404" in missing["data"]
    assert "No schema 999999" in missing["data"]

    invalid = call_tool(mcp_client, "lint_questions", {"questions": {}})
    assert invalid["is_error"] is True
    assert "422" in invalid["data"]


def test_logs_tools_read_what_the_panel_just_did(mcp_client):
    """The loop the whole feature exists for: act, then read the log of the act."""
    call_tool(mcp_client, "schemas_list")

    timeline = json.loads(call_tool(mcp_client, "request_log", {"path": "/api/schemas"})["data"])
    assert timeline["records"], "the MCP-driven request should be in the timeline"
    # Attribution: the record says an agent made this call, not the UI.
    assert any(record.get("mcp_tool") == "schemas_list" for record in timeline["records"])

    searched = json.loads(call_tool(mcp_client, "logs_search", {"q": "schemas_list"})["data"])
    assert searched["count"] >= 1

    stats = json.loads(call_tool(mcp_client, "log_stats")["data"])
    assert stats["log_dir"] == str(get_store().directory)


def test_errors_tools_group_and_open_a_recorded_exception(mcp_client):
    store = get_store()
    store.write(
        {
            "ts": "2026-01-01T00:00:00.000+00:00",
            "kind": "exception",
            "level": "ERROR",
            "logger": "laya_studio.request",
            "message": "POST /api/thing raised ValueError: bad row",
            "path": "/api/thing",
            "request_id": "abc123",
            "exc": {
                "type": "ValueError",
                "message": "bad row",
                "location": "app/bulk.py:42",
                "fingerprint": "deadbeefdeadbeef",
                "traceback": "Traceback (most recent call last):\nValueError: bad row\n",
            },
        }
    )

    groups = json.loads(call_tool(mcp_client, "errors_list")["data"])["groups"]
    group = next(item for item in groups if item["fingerprint"] == "deadbeefdeadbeef")
    assert group["type"] == "ValueError"
    assert group["last_request_id"] == "abc123"

    detail = json.loads(
        call_tool(mcp_client, "errors_detail", {"fingerprint": "deadbeefdeadbeef"})["data"]
    )
    assert "ValueError: bad row" in detail["records"][0]["exc"]["traceback"]


def test_health_and_readiness_report_the_panel(mcp_client):
    health = json.loads(call_tool(mcp_client, "health")["data"])
    assert health["app"] == "Laya Studio"
    assert health["database"]["reachable"] is True
    assert health["database"]["tables"]["schemas"] >= 4

    readiness = json.loads(call_tool(mcp_client, "readiness")["data"])
    # A 503 here is an answer, not a tool failure: the engine double is installed,
    # so this panel is ready and the status is carried in the payload.
    assert readiness["http_status"] == 200
    assert readiness["detail"]["ready"] is True


def test_db_query_reads_the_panel_database(mcp_client):
    result = call_tool(
        mcp_client, "db_query", {"sql": "SELECT key FROM eval_schemas ORDER BY key"}
    )
    payload = json.loads(result["data"])

    assert result["is_error"] is False
    assert payload["columns"] == ["key"]
    keys = [row["key"] for row in payload["rows"]]
    assert "email-triage" in keys  # one of the four built-in templates
    assert keys == sorted(keys)
    assert payload["truncated"] is False


def test_db_query_refuses_writes(mcp_client):
    for sql in (
        "DELETE FROM evaluations",
        "UPDATE eval_schemas SET name = 'x'",
        "SELECT 1; DROP TABLE evaluations",
        "TRUNCATE evaluations",
        "CREATE TABLE t (id int)",
        "SELECT 1 -- comment\n; SELECT 2",
    ):
        result = call_tool(mcp_client, "db_query", {"sql": sql})
        assert result["is_error"] is True, sql
        message = result["data"]
        assert (
            "read-only" in message or "not allowed" in message or "One statement" in message
        ), (sql, message)


def test_db_query_keeps_literals_from_tripping_the_guard(mcp_client):
    """A value that merely looks like a keyword must not be refused."""
    result = call_tool(
        mcp_client,
        "db_query",
        {"sql": "SELECT 'delete me' AS note, name FROM eval_schemas ORDER BY name LIMIT 1"},
    )
    payload = json.loads(result["data"])
    assert result["is_error"] is False
    assert payload["rows"][0]["note"] == "delete me"


def test_db_query_caps_rows_and_says_so(mcp_client):
    result = call_tool(mcp_client, "db_query", {"sql": "SELECT 1 AS n FROM generate_series(1, 50)", "limit": 5})
    payload = json.loads(result["data"])
    assert payload["row_count"] == 5
    assert payload["truncated"] is True


# --- authentication ----------------------------------------------------------


def test_mcp_token_is_enforced_when_set(fresh_db):
    from app.main import create_app

    with environment(LAYA_STUDIO_MCP_TOKEN="mcp-secret"):
        app = create_app()
        with TestClient(app, base_url=PANEL_ORIGIN) as client:
            assert client.post("/mcp", json=INITIALIZE, headers=JSON_HEADERS).status_code == 401

            wrong = dict(JSON_HEADERS, Authorization="Bearer nope")
            assert client.post("/mcp", json=INITIALIZE, headers=wrong).status_code == 401

            right = dict(JSON_HEADERS, Authorization="Bearer mcp-secret")
            assert client.post("/mcp", json=INITIALIZE, headers=right).status_code == 200

            header_token = dict(JSON_HEADERS, **{"X-MCP-Token": "mcp-secret"})
            assert client.post("/mcp", json=INITIALIZE, headers=header_token).status_code == 200


def test_mcp_can_be_switched_off(fresh_db):
    from app.main import create_app

    with environment(LAYA_STUDIO_MCP_ENABLED="false"):
        app = create_app()
        assert getattr(app.state, "mcp_server", None) is None
        assert not any(getattr(route, "path", None) == "/mcp" for route in app.router.routes)
