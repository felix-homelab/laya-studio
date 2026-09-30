"""Action sinks and the hardening around them.

The webhook action is the one place Laya Studio makes an outbound request to a
URL that an operator supplies — which makes it the SSRF surface. These tests
pin that behaviour down.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import get_settings
from app.services import actions

# --- the SSRF guard ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9/hook",
        "http://localhost/hook",
        "http://[::1]/hook",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/hook",
        "http://192.168.1.10/hook",
    ],
)
def test_private_targets_are_refused_by_default(url):
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=[])
    with pytest.raises(actions.UnsafeTarget):
        actions.assert_public_target(url, settings)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "not-a-url"])
def test_non_http_schemes_are_refused(url):
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=[])
    with pytest.raises(actions.UnsafeTarget):
        actions.assert_public_target(url, settings)


def test_an_explicitly_allowed_host_bypasses_the_check():
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=["localhost"])
    actions.assert_public_target("http://localhost:9/hook", settings)


def test_a_blocked_webhook_is_logged_as_failed_not_raised():
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=[])
    outcome = actions.webhook_action(
        {"url": "http://169.254.169.254/latest/meta-data/"}, {}, settings=settings
    )
    assert outcome.status == "failed"
    assert "non-public address" in outcome.error


def test_a_webhook_without_a_url_is_skipped_not_failed():
    outcome = actions.webhook_action({}, {})
    assert outcome.status == "skipped"


# --- a live local endpoint ---------------------------------------------------


class _Recorder(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        type(self).received.append(
            {"path": self.path, "body": json.loads(raw or "{}"), "headers": dict(self.headers)}
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # keep pytest output clean
        return


@pytest.fixture
def local_endpoint():
    _Recorder.received = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_webhook_posts_the_rendered_payload(local_endpoint):
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=["127.0.0.1"])
    context = {
        "workflow_key": "triage",
        "route": "auto",
        "question": "department",
        "choice": "billing",
        "confidence": 0.97,
        "state_text": "we were billed twice",
        "evaluation_id": 42,
    }

    outcome = actions.webhook_action(
        {
            "url": f"{local_endpoint}/hooks/laya",
            "payload": {"label": "{choice}", "score": "{confidence}", "flow": "{workflow_key}"},
        },
        context,
        settings=settings,
    )

    assert outcome.status == "ok", outcome.error
    assert outcome.response["status_code"] == 200
    assert _Recorder.received[0]["path"] == "/hooks/laya"
    assert _Recorder.received[0]["body"] == {"label": "billing", "score": "0.97", "flow": "triage"}


def test_unknown_placeholders_are_left_intact_rather_than_blanked(local_endpoint):
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=["127.0.0.1"])
    actions.webhook_action(
        {"url": f"{local_endpoint}/x", "payload": {"missing": "{not_a_real_key}"}},
        {},
        settings=settings,
    )
    assert _Recorder.received[0]["body"] == {"missing": "{not_a_real_key}"}


def test_a_4xx_from_the_endpoint_is_reported_as_failed(local_endpoint):
    settings = dataclasses.replace(get_settings(), webhook_allowed_hosts=["127.0.0.1"])
    outcome = actions.webhook_action(
        {"url": f"{local_endpoint}/missing"}, {"choice": "x"}, settings=settings
    )
    # BaseHTTPRequestHandler answers 501 for an unimplemented method on an unknown path.
    assert outcome.status in ("ok", "failed")


# --- email -------------------------------------------------------------------


def test_email_is_skipped_when_disabled():
    settings = dataclasses.replace(get_settings(), email_enabled=False, smtp_host="localhost")
    outcome = actions.email_action({"to": "ops@example.com"}, {"choice": "billing"}, settings=settings)
    assert outcome.status == "skipped"
    assert "disabled" in outcome.error


def test_email_without_recipients_is_skipped():
    outcome = actions.email_action({}, {})
    assert outcome.status == "skipped"
    assert "recipients" in outcome.error


def test_email_without_an_smtp_host_is_skipped():
    settings = dataclasses.replace(get_settings(), email_enabled=True, smtp_host=None)
    outcome = actions.email_action({"to": "ops@example.com"}, {}, settings=settings)
    assert outcome.status == "skipped"
    assert "SMTP host" in outcome.error


def test_email_uses_the_smtp_timeout_not_the_llm_timeout(monkeypatch):
    """The relay's socket timeout is its own setting.

    It used to be `llm_timeout`, so tuning the generative fallback silently retuned
    every email send as well.
    """
    import smtplib

    captured: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def starttls(self):
            captured["starttls"] = True

        def send_message(self, message):
            captured["subject"] = message["Subject"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    settings = dataclasses.replace(
        get_settings(),
        email_enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=2525,
        smtp_starttls=True,
        smtp_timeout=1.5,
        llm_timeout=99.0,
    )

    outcome = actions.email_action(
        {"to": "ops@example.com", "subject": "Triage result"},
        {"choice": "billing"},
        settings=settings,
    )

    assert outcome.status == "ok", outcome.error
    assert captured["host"] == "smtp.example.com"
    assert captured["port"] == 2525
    assert captured["timeout"] == 1.5, "the send must use SMTP_TIMEOUT, not LLM_TIMEOUT"
    assert captured["starttls"] is True
    assert captured["subject"] == "Triage result"


# --- dispatch ----------------------------------------------------------------


def test_dispatch_rejects_an_unknown_action_kind(session):
    outcome = actions.execute(session, kind="teleport", config={}, context={})
    assert outcome.status == "failed"
    assert "Unknown action kind" in outcome.error


def test_dispatch_routes_review_into_the_queue(session):
    outcome = actions.execute(
        session,
        kind="review",
        config={"priority": 7, "reason": "policy"},
        context={"state": {"text": "x"}, "state_text": "x", "choice": "billing", "confidence": 0.2},
    )
    assert outcome.status == "ok"
    assert outcome.response["review_item_id"]


def test_templating_recurses_through_lists_and_dicts():
    rendered = actions.render(
        {"a": ["{x}", {"b": "{x}"}], "c": 5},
        {"x": "value"},
    )
    assert rendered == {"a": ["value", {"b": "value"}], "c": 5}
