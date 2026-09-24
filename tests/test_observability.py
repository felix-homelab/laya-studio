"""The JSONL log store, the read-only API over it, and what actually gets written.

Two halves. The store tests drive :class:`~app.observability.store.LogStore`
directly against a tmp directory: rotation, retention, filtering and the
``scanned``/``truncated`` accounting an empty result depends on. The API tests go
through the panel, because the point of the feature is that a *running* panel can
be interrogated — a store that writes records nobody can read back is not
observability, it is an expensive no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from app.config import reload_settings
from app.observability import configure, enabled, get_store
from app.observability.hooks import install_exception_hooks
from app.observability.middleware import RequestLogMiddleware
from app.observability.store import LogStore

# --- helpers -----------------------------------------------------------------


@contextmanager
def environment(**values: str) -> Iterator[None]:
    """Set environment variables and rebuild settings, restoring both afterwards.

    The token tests need the *app* to be built against a patched environment, so
    ``monkeypatch`` is not enough on its own: settings are cached, and the
    restoration has to happen after the cache is dropped again.
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


def log_record(level: str = "INFO", message: str = "hello", **extra: Any) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "kind": "log",
        "level": level,
        "logger": "app.test",
        "message": message,
        **extra,
    }


def exc_record(message: str = "boom", location: str = "app/thing.py:12") -> dict:
    # Real fingerprints are a hex digest of type + raise site, so they are safe to
    # put in a URL path — unlike the raise site itself, which contains slashes.
    fingerprint = hashlib.sha1(f"ValueError|{location}".encode()).hexdigest()[:16]
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "kind": "exception",
        "level": "ERROR",
        "logger": "laya_studio.request",
        "message": message,
        "exc": {
            "type": "ValueError",
            "message": message,
            "location": location,
            "fingerprint": fingerprint,
            "traceback": f"Traceback:\n  ValueError: {message}\n",
        },
    }


# --- the store ---------------------------------------------------------------


def test_write_then_read_round_trips(tmp_path):
    store = LogStore(tmp_path)
    store.write(log_record(message="first"))
    store.write(log_record(level="ERROR", message="second"))

    result = store.query(order="oldest")
    assert [record["message"] for record in result["records"]] == ["first", "second"]
    assert result["truncated"] is False
    assert result["scanned"] == 2

    # One JSON object per line is the whole contract: anything else is not grep-able.
    lines = (tmp_path / store.filename).read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["message"] == "first"


def test_level_is_a_floor_not_an_equality(tmp_path):
    store = LogStore(tmp_path)
    for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        store.write(log_record(level=level, message=level.lower()))

    above_warning = store.query(level="WARNING", order="oldest")
    assert [record["level"] for record in above_warning["records"]] == ["WARNING", "ERROR"]


def test_filters_narrow_the_window(tmp_path):
    store = LogStore(tmp_path)
    store.write(log_record(message="schema updated", path="/api/schemas/1", status=200))
    store.write(log_record(message="workflow failed", path="/api/workflows/2", status=500))
    store.write(exc_record())

    assert store.query(kind="exception")["count"] == 1
    assert store.query(status_min=500)["count"] == 1
    assert store.query(path="/api/schemas")["count"] == 1
    assert store.query(search="WORKFLOW")["count"] == 1  # case-insensitive
    assert store.query(has_exception=True)["count"] == 1
    assert store.query(has_exception=False)["count"] == 2
    assert store.query(logger="laya_studio.request")["count"] == 1


def test_mcp_tool_is_searchable(tmp_path):
    """An agent's call is attributed in the record, and `q` finds it."""
    store = LogStore(tmp_path)
    store.write(log_record(message="POST /api/playground/evaluate -> 200", mcp_tool="evaluate"))
    store.write(log_record(message="POST /api/playground/evaluate -> 200"))

    found = store.query(search="evaluate", order="oldest")
    assert found["count"] == 2  # both match the message
    assert found["records"][0].get("mcp_tool") == "evaluate"
    assert found["records"][1].get("mcp_tool") is None


def test_timestamps_bound_the_query(tmp_path):
    store = LogStore(tmp_path)
    now = datetime.now(timezone.utc)
    store.write(log_record(message="old") | {"ts": (now - timedelta(hours=2)).isoformat()})
    store.write(log_record(message="new"))

    recent = store.query(since=(now - timedelta(minutes=5)).isoformat())
    assert [record["message"] for record in recent["records"]] == ["new"]

    earlier = store.query(until=(now - timedelta(hours=1)).isoformat())
    assert [record["message"] for record in earlier["records"]] == ["old"]


def test_limit_reports_that_it_truncated(tmp_path):
    store = LogStore(tmp_path)
    for index in range(10):
        store.write(log_record(message=f"line {index}"))

    result = store.query(limit=3)
    assert result["count"] == 3
    assert result["truncated"] is True
    assert result["scanned"] == 10
    # Newest first by default.
    assert result["records"][0]["message"] == "line 9"


def test_tracebacks_are_opt_in_and_capped(tmp_path):
    store = LogStore(tmp_path)
    store.write(exc_record())

    without = store.query()["records"][0]["exc"]
    assert "traceback" not in without
    assert without["traceback_chars"] > 0

    with_traceback = store.query(include_traceback=True)["records"][0]["exc"]
    assert "ValueError: boom" in with_traceback["traceback"]


def test_error_groups_count_and_time_the_same_failure(tmp_path):
    store = LogStore(tmp_path)
    for _ in range(3):
        store.write(exc_record(message="row 1 is broken"))
    store.write(exc_record(message="a different thing", location="app/other.py:3"))

    groups = store.error_groups()
    assert groups["count"] == 2
    repeated = next(
        group for group in groups["groups"] if group["location"] == "app/thing.py:12"
    )
    # Same type and raise site collapse, even though the messages differ — which
    # is the point: 4 000 failed rows are one bug, not 4 000.
    assert repeated["count"] == 3
    assert repeated["type"] == "ValueError"
    assert "traceback" not in repeated  # not asked for
    assert repeated["first_seen"] <= repeated["last_seen"]


def test_rotation_keeps_the_chain_bounded(tmp_path):
    store = LogStore(tmp_path, max_bytes=64 * 1024, backup_count=2)
    # The constructor clamps max_bytes to a sane floor; drop it for the test so
    # rotation can be exercised with a handful of records.
    store.max_bytes = 200
    for index in range(20):
        store.write(log_record(message=f"record {index} " + "x" * 50))

    rotated = sorted(item.name for item in tmp_path.iterdir())
    assert store.filename in rotated
    assert f"{store.filename}.1" in rotated
    # backup_count is a ceiling: the oldest file is deleted, not renamed forever.
    assert f"{store.filename}.3" not in rotated


def test_retention_prunes_old_files(tmp_path):
    store = LogStore(tmp_path, retention_days=1)
    old = tmp_path / "laya-studio-9999.jsonl"
    old.write_text("{}\n")
    ancient = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
    os.utime(old, (ancient, ancient))

    assert store.prune() == 1
    assert not old.exists()


def test_an_unparseable_line_is_kept_as_text(tmp_path):
    """A half-written line from a killed process is often the interesting one."""
    store = LogStore(tmp_path)
    store.path.write_text('{"ts": "2026-01-01T00:00:00.000+00:00", "level": "INFO", '
                          '"kind": "log", "logger": "x", "message": "good"}\ntruncated json{\n')

    records = store.query(order="oldest")["records"]
    assert records[0]["message"] == "good"
    assert records[1]["logger"] == "laya_studio.unparsed"
    assert records[1]["message"] == "truncated json{"


def test_stats_reports_the_files_and_the_window(tmp_path):
    store = LogStore(tmp_path)
    store.write(log_record())
    store.write(log_record(level="ERROR"))

    stats = store.stats()
    assert stats["log_dir"] == str(tmp_path)
    assert stats["counts_by_level"]["INFO"] == 1
    assert stats["counts_by_level"]["ERROR"] == 1
    assert stats["total_bytes"] > 0
    assert stats["retention_days"] == 14


def test_write_never_raises_when_the_directory_is_unusable(tmp_path):
    """Observability must not be able to fail a request."""
    store = LogStore(tmp_path / "not-a-directory")
    (tmp_path / "not-a-directory").write_text("in the way")

    store.write(log_record())  # must not raise


# --- capture -----------------------------------------------------------------


def test_middleware_records_a_request_with_status_and_duration(tmp_path):
    store = LogStore(tmp_path)

    async def ok(_request):
        from starlette.responses import JSONResponse

        return JSONResponse({"fine": True})

    app = RequestLogMiddleware(Starlette(routes=[Route("/api/thing", ok)]), store)
    with TestClient(app) as client:
        response = client.get("/api/thing")
        assert response.status_code == 200
        # The correlation id comes back on the response, so a user can quote it.
        assert response.headers["X-Request-ID"]

    record = store.query(kind="request")["records"][0]
    assert record["path"] == "/api/thing"
    assert record["status"] == 200
    assert record["request_id"] == response.headers["X-Request-ID"]
    assert record["duration_ms"] >= 0


def test_middleware_records_an_exception_with_its_traceback(tmp_path):
    store = LogStore(tmp_path)

    async def boom(_request):
        raise ValueError("kaboom")

    app = RequestLogMiddleware(Starlette(routes=[Route("/api/boom", boom)]), store)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/boom").status_code == 500

    records = store.query(kind="exception", include_traceback=True)["records"]
    assert len(records) == 1
    assert records[0]["exc"]["type"] == "ValueError"
    assert "kaboom" in records[0]["exc"]["traceback"]
    assert "test_observability.py" in records[0]["exc"]["location"]


def test_middleware_skips_probes_and_static_assets(tmp_path):
    store = LogStore(tmp_path)

    async def ok(_request):
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    app = RequestLogMiddleware(
        Starlette(
            routes=[
                Route("/api/health", ok),
                Route("/api/ready", ok),
                Route("/static/x.css", ok),
            ]
        ),
        store,
    )
    with TestClient(app) as client:
        client.get("/api/health")
        client.get("/api/ready")
        client.get("/static/x.css")

    assert store.query()["count"] == 0


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_thread_hook_records_a_background_failure(tmp_path):
    """The engine loader runs in a thread: a torch failure has to reach the store."""
    store = LogStore(tmp_path)
    install_exception_hooks(store)

    try:
        raise RuntimeError("the loader thread died")
    except RuntimeError as exc:
        threading.excepthook(
            threading.ExceptHookArgs((type(exc), exc, exc.__traceback__, None))
        )

    groups = store.error_groups(include_traceback=True)["groups"]
    assert groups[0]["type"] == "RuntimeError"
    assert "loader thread died" in groups[0]["traceback"]


# --- the API -----------------------------------------------------------------


def test_logs_search_reads_what_the_panel_wrote(client):
    store = get_store()
    logging.getLogger("app.test.observability").warning("a distinctive marker")

    response = client.get("/api/observability/logs", params={"q": "distinctive marker"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 1
    assert body["records"][0]["level"] == "WARNING"
    assert body["scanned"] >= body["count"]
    assert store.directory.exists()


def test_requests_endpoint_shows_the_timeline(client):
    client.get("/api/schemas")

    response = client.get("/api/observability/requests", params={"path": "/api/schemas"})
    assert response.status_code == 200
    records = response.json()["records"]
    assert records, "the schemas request should have been recorded"
    assert records[0]["status"] == 200
    assert records[0]["method"] == "GET"


def test_tail_and_stats(client):
    logging.getLogger("app.test.observability").info("tail me")

    tail = client.get("/api/observability/logs/tail", params={"lines": 5}).json()
    assert tail["count"] >= 1

    stats = client.get("/api/observability/stats").json()
    assert stats["current_file"].endswith(".jsonl")
    assert "counts_by_kind" in stats


def test_errors_endpoint_groups_and_details_one_fingerprint(client):
    store = get_store()
    record = exc_record(message="something failed", location="app/api/thing.py:7")
    store.write(record)
    fingerprint = record["exc"]["fingerprint"]

    listed = client.get("/api/observability/errors", params={"include_traceback": True}).json()
    group = next(item for item in listed["groups"] if item["fingerprint"] == fingerprint)
    assert group["count"] == 1
    assert "ValueError" in group["traceback"]

    detail = client.get(f"/api/observability/errors/{fingerprint}").json()
    assert detail["type"] == "ValueError"
    assert detail["occurrences"] == 1
    assert detail["records"][0]["exc"]["traceback"]


def test_unknown_fingerprint_is_a_404_with_a_hint(client):
    response = client.get("/api/observability/errors/nope")
    assert response.status_code == 404
    assert "/api/observability/errors" in response.json()["detail"]


def test_observability_token_gates_the_endpoints(client):
    with environment(LAYA_STUDIO_OBSERVABILITY_TOKEN="obs-secret"):
        assert client.get("/api/observability/stats").status_code == 401
        assert (
            client.get(
                "/api/observability/stats", headers={"X-Observability-Token": "wrong"}
            ).status_code
            == 401
        )
        allowed = client.get(
            "/api/observability/stats", headers={"X-Observability-Token": "obs-secret"}
        )
        assert allowed.status_code == 200


def test_jsonl_capture_can_be_turned_off(tmp_path):
    with environment(
        LAYA_STUDIO_LOG_JSONL="false", LAYA_STUDIO_LOG_DIR=str(tmp_path / "disabled-logs")
    ):
        store = configure()
        assert enabled() is False
        # Nothing is created while the sink is off: no directory, no files, and
        # stderr is still the whole story.
        assert not store.directory.exists()
    configure()  # put the session's store back the way the other tests expect it


def test_the_log_endpoints_are_mounted_and_documented(client):
    """Behaviour, not route internals: this FastAPI wraps included routers."""
    assert client.get("/api/observability/stats").status_code == 200

    spec = client.get("/api/openapi.json").json()
    assert "/api/observability/logs" in spec["paths"]
    assert "/api/observability/errors/{fingerprint}" in spec["paths"]

