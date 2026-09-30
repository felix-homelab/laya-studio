"""Downstream action sinks: webhooks, email, the review queue, and no-ops.

Every action returns an :class:`ActionOutcome` and is persisted as an
:class:`~app.models.ActionLog` row, so the workflow visualizer can show exactly
what fired, what it sent, and what came back.

Actions never raise into the caller: a failing webhook should surface on the run
as ``status="failed"``, not blow up the ingestion request that triggered it.
"""

from __future__ import annotations

import ipaddress
import logging
import smtplib
import socket
import string
from dataclasses import dataclass, field
from email.message import EmailMessage
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import ActionLog, Evaluation, Workflow, WorkflowRun

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActionOutcome:
    kind: str
    status: str = "ok"
    target: str = ""
    detail: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "target": self.target,
            "detail": self.detail,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 3),
        }


class UnsafeTarget(ValueError):
    """The webhook target resolved to a non-public address."""


# ---------------------------------------------------------------------------
# Templating
# ---------------------------------------------------------------------------


class _SafeDict(dict):
    """``str.format_map`` helper that leaves unknown placeholders intact."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class _FlatFormatter(string.Formatter):
    """Plain ``{name}`` lookups only.

    Templates are operator-authored, so ``{state.items}``-style attribute and
    ``{state[key]}`` index traversal is refused rather than letting a config walk
    into object internals.
    """

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        if "." in field_name or "[" in field_name:
            raise KeyError(field_name)
        return super().get_field(field_name, args, kwargs)


def render(value: Any, context: dict[str, Any]) -> Any:
    """Recursively substitute ``{placeholder}`` in a config structure."""
    if isinstance(value, str):
        try:
            return _FlatFormatter().vformat(value, (), _SafeDict(context))
        except (ValueError, KeyError, IndexError, AttributeError, TypeError):
            # e.g. a bad format spec, or ``{confidence:d}`` on a float.
            return value
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    return value


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


def assert_public_target(url: str, settings: Settings | None = None) -> str | None:
    """Refuse webhook targets that point inside the network.

    Ingestion is unauthenticated by default, so an unguarded webhook action is a
    server-side request forgery primitive against the cloud metadata service and
    any internal admin surface. Hosts explicitly listed in
    ``LAYA_STUDIO_WEBHOOK_ALLOWED_HOSTS`` bypass the check.

    Returns the vetted address the request must connect to (so a second DNS
    lookup at connect time cannot be rebound to an internal address), or ``None``
    for an allow-listed host, which is resolved normally.
    """
    settings = settings or get_settings()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise UnsafeTarget(f"Unsupported webhook scheme: {parsed.scheme or '(none)'}")
    host = parsed.hostname
    if not host:
        raise UnsafeTarget("Webhook URL has no host.")

    if host in settings.webhook_allowed_hosts:
        return None

    try:
        # Ordered and de-duplicated, so the pinned address is the resolver's first choice.
        addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, None)))
    except socket.gaierror as exc:
        raise UnsafeTarget(f"Could not resolve webhook host {host!r}: {exc}") from exc

    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo returns valid IPs
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeTarget(
                f"Webhook host {host!r} resolves to non-public address {address}. "
                "Add it to LAYA_STUDIO_WEBHOOK_ALLOWED_HOSTS if this is intentional."
            )

    if not addresses:  # pragma: no cover - getaddrinfo raises rather than return nothing
        raise UnsafeTarget(f"Could not resolve webhook host {host!r}.")
    return addresses[0]


# ---------------------------------------------------------------------------
# Individual actions
# ---------------------------------------------------------------------------

#: Verbs a webhook step may use; only the body-carrying ones send the payload.
WEBHOOK_METHODS = ("POST", "PUT", "PATCH", "DELETE", "GET")
_BODY_METHODS = ("POST", "PUT", "PATCH")


def webhook_action(
    config: dict[str, Any],
    context: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> ActionOutcome:
    settings = settings or get_settings()
    url = str(config.get("url") or "")
    if not url:
        return ActionOutcome(kind="webhook", status="skipped", error="No webhook URL configured.")

    payload = render(config.get("payload") or _default_payload(context), context)
    method = str(config.get("method", "POST")).upper()
    headers = {"Content-Type": "application/json"}
    headers.update({str(key): str(value) for key, value in (config.get("headers") or {}).items()})

    outcome = ActionOutcome(kind="webhook", target=url, request={"method": method, "payload": payload})

    if method not in WEBHOOK_METHODS:
        outcome.status = "failed"
        outcome.error = f"Unsupported webhook method {method!r}; expected one of {', '.join(WEBHOOK_METHODS)}."
        return outcome

    try:
        address = assert_public_target(url, settings)
    except UnsafeTarget as exc:
        outcome.status = "failed"
        outcome.error = str(exc)
        return outcome

    started = perf_counter()
    try:
        request_url: httpx.URL | str = url
        extensions: dict[str, Any] = {}
        if address is not None:
            # Connect to the address that was vetted rather than letting httpx
            # resolve the name again (a DNS-rebinding window), while keeping the
            # original name in the Host header and in TLS SNI/certificate checks.
            original = httpx.URL(url)
            request_url = original.copy_with(host=address)
            if not any(key.lower() == "host" for key in headers):
                headers["Host"] = original.netloc.decode("ascii")
            extensions["sni_hostname"] = original.raw_host.decode("ascii")

        with httpx.Client(timeout=settings.webhook_timeout) as client:
            response = client.request(
                method,
                request_url,
                json=payload if method in _BODY_METHODS else None,
                headers=headers,
                extensions=extensions,
            )
        outcome.latency_ms = (perf_counter() - started) * 1000.0
        outcome.status = "ok" if response.is_success else "failed"
        outcome.response = {
            "status_code": response.status_code,
            "body": response.text[:2000],
        }
        outcome.detail = f"{response.status_code} in {outcome.latency_ms:.0f}ms"
        if not response.is_success:
            outcome.error = f"Endpoint returned {response.status_code}."
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        outcome.latency_ms = (perf_counter() - started) * 1000.0
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def _default_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "workflow": context.get("workflow_key"),
        "route": context.get("route"),
        "question": context.get("question"),
        "choice": context.get("choice"),
        "confidence": context.get("confidence"),
        "state": context.get("state_text"),
        "evaluation_id": context.get("evaluation_id"),
    }


def email_action(
    config: dict[str, Any],
    context: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> ActionOutcome:
    settings = settings or get_settings()
    recipients = config.get("to") or []
    if isinstance(recipients, str):
        recipients = [item.strip() for item in recipients.split(",") if item.strip()]
    recipients = [str(item) for item in recipients]

    if not recipients:
        return ActionOutcome(kind="email", status="skipped", error="No recipients configured.")

    subject = render(str(config.get("subject", "Laya Studio workflow: {route}")), context)
    body = render(str(config.get("body", _default_email_body(context))), context)

    outcome = ActionOutcome(
        kind="email",
        target=", ".join(recipients),
        request={"subject": subject, "recipients": recipients, "body": body[:2000]},
    )

    if not settings.email_enabled:
        outcome.status = "skipped"
        outcome.error = "Email is disabled (LAYA_STUDIO_EMAIL_ENABLED=false)."
        return outcome
    if not settings.smtp_host:
        outcome.status = "skipped"
        outcome.error = "No SMTP host configured."
        return outcome

    message = EmailMessage()
    message["From"] = settings.smtp_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)

    started = perf_counter()
    try:
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout
        ) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
        outcome.latency_ms = (perf_counter() - started) * 1000.0
        outcome.detail = f"sent to {len(recipients)} recipient(s)"
    except Exception as exc:  # noqa: BLE001 - SMTP failures are myriad
        outcome.latency_ms = (perf_counter() - started) * 1000.0
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
    return outcome


def _default_email_body(context: dict[str, Any]) -> str:
    return (
        "A Laya Studio workflow routed an item to you.\n\n"
        "Workflow : {workflow_key}\n"
        "Route    : {route}\n"
        "Question : {question}\n"
        "Answer   : {choice}\n"
        "Confidence: {confidence}\n\n"
        "State:\n{state_text}\n"
    )


def review_action(
    db: Session,
    config: dict[str, Any],
    context: dict[str, Any],
    *,
    commit: bool = False,
) -> ActionOutcome:
    """Park the item in the manual review queue."""
    from . import review as review_service

    outcome = ActionOutcome(kind="review")
    try:
        item = review_service.enqueue(
            db,
            workflow_id=context.get("workflow_id"),
            workflow_run_id=context.get("workflow_run_id"),
            evaluation_id=context.get("evaluation_id"),
            state=context.get("state") or {},
            state_text=str(context.get("state_text") or ""),
            question=context.get("question"),
            model_choice=context.get("choice"),
            model_confidence=context.get("confidence"),
            alternatives=context.get("alternatives") or [],
            reason=str(config.get("reason") or context.get("reason") or "low_confidence"),
            # `is not None`, not `or`: an explicit priority of 0 must win over the
            # route default in the context.
            priority=int(
                config["priority"]
                if config.get("priority") is not None
                else context.get("priority") or 0
            ),
            commit=commit,
        )
    except Exception as exc:  # noqa: BLE001 - never break the run
        logger.exception("Failed to enqueue a review item")
        outcome.status = "failed"
        outcome.error = f"{type(exc).__name__}: {exc}"
        return outcome

    outcome.target = f"review:{item.id}"
    outcome.detail = f"queued for human review (reason={item.reason})"
    outcome.response = {"review_item_id": item.id}
    return outcome


def noop_action(config: dict[str, Any], context: dict[str, Any]) -> ActionOutcome:
    detail = str(config.get("detail") or "No action configured.")
    return ActionOutcome(kind="noop", status="ok", detail=detail, response=render(config, context))


# ---------------------------------------------------------------------------
# Dispatch + persistence
# ---------------------------------------------------------------------------

SUPPORTED_ACTIONS = ("webhook", "email", "review", "noop")


def execute(
    db: Session,
    *,
    kind: str,
    config: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    commit: bool = False,
) -> ActionOutcome:
    """Run one action by name and return its outcome."""
    config = config or {}
    context = context or {}
    kind = str(kind or "noop")

    if kind == "webhook":
        return webhook_action(config, context, settings=settings)
    if kind == "email":
        return email_action(config, context, settings=settings)
    if kind == "review":
        return review_action(db, config, context, commit=commit)
    if kind == "noop":
        return noop_action(config, context)

    return ActionOutcome(
        kind=kind,
        status="failed",
        error=f"Unknown action kind {kind!r}; expected one of {', '.join(SUPPORTED_ACTIONS)}.",
    )


def log_outcome(
    db: Session,
    outcome: ActionOutcome,
    *,
    run: WorkflowRun | None = None,
    commit: bool = False,
) -> ActionLog:
    """Persist an outcome as an :class:`~app.models.ActionLog` row."""
    entry = ActionLog(
        workflow_run_id=run.id if run is not None else None,
        kind=outcome.kind,
        target=outcome.target[:500],
        status=outcome.status,
        request=outcome.request,
        response=outcome.response,
        error=outcome.error,
        latency_ms=outcome.latency_ms,
    )
    db.add(entry)
    if commit:
        db.commit()
        db.refresh(entry)
    else:
        db.flush()
    return entry


def build_context(
    *,
    workflow: Workflow | None,
    evaluation: Evaluation | None,
    route: str,
    question: str | None,
    choice: str | None,
    confidence: float | None,
    state_text: str,
    reason: str | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    landing_route: str | None = None,
    run: WorkflowRun | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the template context handed to every action.

    ``route`` is the rung the confidence ladder chose, and it is what ``{route}``
    renders. ``landing_route`` is where the run actually ended up once verification
    had its say, and it defaults to ``route``. They differ exactly when the middle
    band was adjudicated: a failed or unavailable fallback escalates into ``review``,
    an agreeing one settles in ``auto``. The default review priority follows the
    *landing* route, so an escalated item is queued with the same weight as one the
    ladder sent straight there — see the ``{priority}`` row in ``docs/workflows.md``.
    """
    landing = landing_route or route
    context: dict[str, Any] = {
        "workflow_id": workflow.id if workflow is not None else None,
        "workflow_key": workflow.key if workflow is not None else None,
        "workflow_name": workflow.name if workflow is not None else None,
        "workflow_run_id": run.id if run is not None else None,
        "evaluation_id": evaluation.id if evaluation is not None else None,
        "route": route,
        "question": question,
        "choice": choice,
        "confidence": None if confidence is None else round(confidence, 6),
        "state": evaluation.state if evaluation is not None else {},
        "state_text": state_text,
        "reason": reason,
        "alternatives": alternatives or [],
        "landing_route": landing,
        "priority": 100 if landing == "review" else 0,
    }
    if extra:
        context.update(extra)
    return context
