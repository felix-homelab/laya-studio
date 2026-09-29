"""Request-scoped context that follows a log record out of the request.

A log line is far more useful when it says *which* request produced it and *what*
happened next, but the standard ``logging`` module has no request concept. The
usual fixes — threading the id through every function, or only passing it
explicitly — are worse than the problem.

So the context lives in a :mod:`contextvars` variable. The request middleware
binds it once, and
:class:`~app.observability.handler.JsonlLogHandler` reads it when shaping a
record. Context variables are copied per task, so concurrent async requests do
not leak into each other, and a background thread starts with an empty context
rather than inheriting whatever request happened to be in flight.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("laya_studio_log_context", default=None)


def new_request_id() -> str:
    """A short, log-friendly correlation id."""
    return uuid.uuid4().hex[:16]


def bind(**fields: Any) -> Token:
    """Merge ``fields`` into the current context and return a reset token."""
    merged = {**(_context.get() or {}), **{key: value for key, value in fields.items() if value is not None}}
    return _context.set(merged)


def reset(token: Token) -> None:
    """Restore the context captured by :func:`bind`."""
    _context.reset(token)


def current() -> dict[str, Any] | None:
    """A copy of the active context, or ``None`` outside a request.

    A copy, so a caller that edits what it gets back cannot change the context for
    the rest of the request.
    """
    context = _context.get()
    return dict(context) if context is not None else None
