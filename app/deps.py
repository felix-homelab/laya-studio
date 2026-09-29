"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from .config import get_settings


def _check_token(provided: str | None, expected: str | None, header: str) -> None:
    """Reject the request unless ``provided`` matches ``expected``.

    An unset ``expected`` leaves the surface open. Both sides are compared as
    UTF-8 bytes: :func:`hmac.compare_digest` raises ``TypeError`` on a ``str``
    with non-ASCII characters, and a header value is whatever the client sent.
    """
    if not expected:
        return
    # Constant-time compare so the token cannot be recovered byte-by-byte.
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {header}.",
        )


def require_ingest_token(
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> None:
    """Gate the ingestion endpoints behind a shared token.

    Ingestion is the one surface designed to be called by third parties, so it is
    the one that needs authenticating. When ``LAYA_STUDIO_INGEST_TOKEN`` is unset
    the endpoints are open — appropriate for a local panel, and called out
    prominently in the UI and in ``docs/deployment.md``.
    """
    _check_token(x_ingest_token, get_settings().ingest_token, "X-Ingest-Token")


def require_observability_token(
    x_observability_token: Annotated[str | None, Header(alias="X-Observability-Token")] = None,
) -> None:
    """Gate the log-reading endpoints behind a shared token.

    These endpoints return log messages and tracebacks, and a traceback carries
    whatever the failing code had in hand — a row of a dataset, a DSN, a webhook
    URL. On a local panel that is exactly what you want to be able to read without
    ceremony, so an unset token leaves them open, the same trade-off
    :func:`require_ingest_token` makes. Set ``LAYA_STUDIO_OBSERVABILITY_TOKEN``
    before exposing the panel to anything else.
    """
    _check_token(
        x_observability_token, get_settings().observability_token, "X-Observability-Token"
    )
