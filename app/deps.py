"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Header, HTTPException, status

from .config import get_settings


def require_ingest_token(
    x_ingest_token: Annotated[str | None, Header(alias="X-Ingest-Token")] = None,
) -> None:
    """Gate the ingestion endpoints behind a shared token.

    Ingestion is the one surface designed to be called by third parties, so it is
    the one that needs authenticating. When ``LAYA_STUDIO_INGEST_TOKEN`` is unset
    the endpoints are open — appropriate for a local panel, and called out
    prominently in the UI and in ``docs/deployment.md``.
    """
    expected = get_settings().ingest_token
    if not expected:
        return
    # Constant-time compare so the token cannot be recovered byte-by-byte.
    if not x_ingest_token or not hmac.compare_digest(x_ingest_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Ingest-Token.",
        )


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
    expected = get_settings().observability_token
    if not expected:
        return
    if not x_observability_token or not hmac.compare_digest(x_observability_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Observability-Token.",
        )
