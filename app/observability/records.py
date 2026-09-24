"""Shaping: :class:`logging.LogRecord` → JSON object, and exception fingerprints.

Three kinds of record reach the store, all with the same outer shape so a single
query can read them:

``log``
    A normal logging call. Carries ``level``, ``logger``, ``message`` and any
    ``extra=`` fields.
``request``
    One HTTP request, written by the middleware *after* the response starts, so it
    carries the status and the duration. Requests are written by the middleware
    rather than through ``logging`` on purpose: the panel's own traffic would
    otherwise double every line in stderr.
``exception``
    A request that raised. Same fields as ``request`` plus ``exc``, which is what
    makes these worth keeping: a status alone does not tell you *where* it broke.

Every record is stamped with the active request context (see
:mod:`app.observability.context`) and, when it carries a traceback, with a
:func:`fingerprint` so repeated failures group instead of filling the log.
"""

from __future__ import annotations

import hashlib
import traceback
from datetime import datetime, timezone
from typing import Any

from .context import current

#: Attributes the logging module puts on every record. Anything outside this set
#: arrived through ``extra=`` and is preserved verbatim.
_STANDARD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "message", "module", "msecs", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info", "stacklevel",
        "taskName", "thread", "threadName",
    }
)

#: Levels, ordered, so a query can ask for "WARNING and above".
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LEVEL_RANK = {name: index for index, name in enumerate(LEVELS)}


def utc_now() -> str:
    """The timestamp format every record uses: UTC, millisecond precision.

    Fixed-width UTC ISO-8601 sorts lexicographically, so the store can filter by
    time by comparing strings — no parsing on the hot path.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_timestamp(value: str | None) -> str | None:
    """Coerce a caller-supplied time into the store's format, or ``None``."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def fingerprint(exc_type: str, location: str | None) -> str:
    """A stable id for "this failure, again".

    Built from the exception type and the raise site only. Deliberately not from
    the message: the message is usually the thing that varies between occurrences
    (an id, a row number, a DSN), and folding it in would make every occurrence a
    new group — which is exactly what you do not want when triaging.
    """
    digest = hashlib.sha1(f"{exc_type}|{location or ''}".encode()).hexdigest()
    return digest[:16]


def exception_payload(exc_info: tuple[Any, BaseException, Any] | None) -> dict[str, Any] | None:
    """Turn ``sys.exc_info()`` into a record-shaped dict."""
    if not exc_info:
        return None
    exc_type, exc, tb = exc_info
    name = getattr(exc_type, "__name__", str(exc_type))
    frames = traceback.extract_tb(tb) if tb is not None else []
    location = None
    if frames:
        last = frames[-1]
        location = f"{last.filename}:{last.lineno}"
    return {
        "type": name,
        "message": str(exc),
        "location": location,
        "fingerprint": fingerprint(name, location),
        "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
    }


def exception_record(exc: BaseException, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """A record for an exception caught outside a logging call."""
    payload = exception_payload((type(exc), exc, exc.__traceback__))
    return {
        "ts": utc_now(),
        "kind": "exception",
        "level": "ERROR",
        "logger": "laya_studio.unhandled",
        "message": f"{type(exc).__name__}: {exc}",
        "exc": payload,
        **(context or {}),
    }


def request_record(
    *,
    kind: str,
    method: str,
    path: str,
    status: int | None,
    duration_ms: float,
    request_id: str,
    query: str = "",
    client: str | None = None,
    exc_info: tuple[Any, Any, Any] | None = None,
    mcp_tool: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One HTTP request — successful, rejected, or raised."""
    level = "INFO"
    if status is None or status >= 500:
        level = "ERROR"
    elif status >= 400:
        level = "WARNING"

    payload: dict[str, Any] = {
        "ts": utc_now(),
        "kind": kind,
        "level": level,
        "logger": "laya_studio.request",
        "message": f"{method} {path} -> {status if status is not None else 'no response'}"
        f" in {duration_ms:.1f}ms",
        "request_id": request_id,
        "method": method,
        "path": path,
        "query": query,
        "status": status,
        "duration_ms": round(duration_ms, 2),
    }
    if client:
        payload["client"] = client
    if mcp_tool:
        payload["mcp_tool"] = mcp_tool
    exc = exception_payload(exc_info)
    if exc:
        payload["exc"] = exc
        payload["message"] = f"{method} {path} raised {exc['type']}: {exc['message']}"
    if extra:
        payload["extra"] = extra
    return payload


def log_record_to_dict(record: Any) -> dict[str, Any]:
    """Flatten a :class:`logging.LogRecord` into the store's shape."""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - a bad %-args must not lose the line
        message = str(record.msg)

    payload: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        "kind": "log",
        "level": record.levelname,
        "logger": record.name,
        "message": message,
        "module": record.module,
        "func": record.funcName,
        "line": record.lineno,
        "thread": record.threadName,
        "process": record.process,
    }

    exc = exception_payload(record.exc_info)
    if exc:
        payload["exc"] = exc
    if getattr(record, "stack_info", None):
        payload["stack"] = record.stack_info

    extras = {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_ATTRS and not key.startswith("_")
    }
    if extras:
        payload["extra"] = extras

    context = current()
    if context:
        payload["request"] = context
    return payload
