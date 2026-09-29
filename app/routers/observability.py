"""The read side of the log store — logs, exceptions and request history.

This is the third door onto the same files (the other two are ``grep`` on the
bind-mounted directory and the MCP tools in :mod:`app.mcp`), and the only one that
works when the panel is not on localhost.

Two design choices worth stating, because both are about not lying to the caller:

* **Every response reports what it looked at.** ``scanned`` is how many records
  were read, ``truncated`` says whether the window or the limit cut the answer
  short. A bare ``{"records": []}`` is indistinguishable from "the log rotated",
  which is the wrong thing to conclude while debugging.
* **Tracebacks are opt-in.** ``include_traceback=false`` is the default so that a
  broad search stays cheap to read and to move over MCP; the exception endpoints
  turn it on, because there the traceback *is* the answer.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..deps import require_observability_token
from ..observability import get_store

router = APIRouter(
    prefix="/api/observability",
    tags=["observability"],
    dependencies=[Depends(require_observability_token)],
)


@router.get("/logs")
def search_logs(
    level: Annotated[str | None, Query(description="Floor: 'INFO' matches INFO and above.")] = None,
    kind: Annotated[str | None, Query(description="log | request | exception")] = None,
    logger: Annotated[str | None, Query(description="Substring match on the logger name.")] = None,
    q: Annotated[str | None, Query(description="Case-insensitive substring on the message.")] = None,
    request_id: str | None = None,
    path: Annotated[str | None, Query(description="Substring match on a request path.")] = None,
    status_min: int | None = None,
    status_max: int | None = None,
    has_exception: bool | None = None,
    fingerprint: str | None = None,
    since: Annotated[str | None, Query(description="ISO-8601, e.g. 2026-09-22T11:00:00Z")] = None,
    until: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    order: Literal["newest", "oldest"] = "newest",
    include_traceback: bool = False,
) -> dict:
    """Search the retained log window."""
    return get_store().query(
        level=level,
        kind=kind,
        logger=logger,
        search=q,
        request_id=request_id,
        path=path,
        status_min=status_min,
        status_max=status_max,
        has_exception=has_exception,
        fingerprint=fingerprint,
        since=since,
        until=until,
        limit=limit,
        order=order,
        include_traceback=include_traceback,
    )


@router.get("/logs/tail")
def tail_logs(
    lines: int = Query(default=50, ge=1, le=1000),
    include_traceback: bool = False,
) -> dict:
    """The most recent records, whatever they are — the first thing to read."""
    # store.query rather than store.tail, so the envelope keeps `scanned` and
    # `truncated` like every other endpoint here.
    result = get_store().query(limit=lines, order="newest", include_traceback=include_traceback)
    return {
        "records": result["records"],
        "count": result["count"],
        "scanned": result["scanned"],
        "truncated": result["truncated"],
    }


@router.get("/errors")
def list_errors(
    since: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    include_traceback: bool = False,
) -> dict:
    """Distinct exceptions, grouped by type and raise site.

    Grouping is the difference between "4 000 failures" and "one
    ``ValidationError`` in the bulk parser, 4 000 times".
    """
    return get_store().error_groups(
        since=since, limit=limit, include_traceback=include_traceback
    )


@router.get("/errors/{fingerprint}")
def error_detail(
    fingerprint: str,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    """Every recorded occurrence of one exception, tracebacks included."""
    store = get_store()
    result = store.query(fingerprint=fingerprint, limit=limit, order="newest",
                         include_traceback=True)
    if not result["records"]:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No exception with fingerprint {fingerprint!r} in the retained window. "
            "List them with GET /api/observability/errors.",
        )
    first = next((record for record in result["records"] if record.get("exc")), {})
    exc = first.get("exc", {})
    return {
        "fingerprint": fingerprint,
        "type": exc.get("type"),
        "location": exc.get("location"),
        "message": exc.get("message"),
        "occurrences": result["count"],
        "scanned": result["scanned"],
        "truncated": result["truncated"],
        "records": result["records"],
    }


@router.get("/requests")
def request_history(
    status_min: int | None = None,
    path: Annotated[str | None, Query(description="Substring match on the request path.")] = None,
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """The request timeline: method, path, status, duration, correlation id."""
    return get_store().query(kind="request", status_min=status_min, path=path, limit=limit)


@router.get("/stats")
def log_stats() -> dict:
    """What is on disk, how far back it goes, and where the files are."""
    return get_store().stats()
