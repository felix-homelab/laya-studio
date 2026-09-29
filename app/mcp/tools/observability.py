"""Tools over the log store — the reason the MCP server exists.

These read the JSONL records described in :mod:`app.observability`. They are what
turns "something is wrong with the panel" into a question an agent can answer
itself: search the window, group the exceptions, open the one that matters, and
walk back to the request that produced it.

Note what is *not* here: any tool that deletes log files. Rotation and retention
own the window; a debugging tool that can erase the evidence is a debugging tool
that lies.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer

from ..annotations import READ
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def logs_search(
        level: str | None = None,
        kind: str | None = None,
        logger: str | None = None,
        q: str | None = None,
        request_id: str | None = None,
        path: str | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        has_exception: bool | None = None,
        fingerprint: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
        order: str = "newest",
        include_traceback: bool = False,
    ) -> Any:
        """Search the panel's retained log window (rotated JSONL, newest first).

        `level` is a floor: "WARNING" returns WARNING, ERROR and CRITICAL. `kind`
        is one of log / request / exception. `q` is a case-insensitive substring
        over the message; `logger`, `path` and `fingerprint` are substring/exact
        filters. Times are ISO-8601 (`2026-09-22T11:00:00Z`). The response carries
        `scanned` and `truncated` so an empty result is never ambiguous — an empty
        `records` list with `truncated: true` means the window, not the code.
        """
        return await panel.get(
            "/api/observability/logs",
            tool="logs_search",
            params={
                "level": level,
                "kind": kind,
                "logger": logger,
                "q": q,
                "request_id": request_id,
                "path": path,
                "status_min": status_min,
                "status_max": status_max,
                "has_exception": has_exception,
                "fingerprint": fingerprint,
                "since": since,
                "until": until,
                "limit": limit,
                "order": order,
                "include_traceback": include_traceback,
            },
        )

    @server.tool(annotations=READ)
    async def logs_tail(lines: int = 50, include_traceback: bool = False) -> Any:
        """The most recent records of any kind — start here when something just broke."""
        return await panel.get(
            "/api/observability/logs/tail",
            tool="logs_tail",
            params={"lines": lines, "include_traceback": include_traceback},
        )

    @server.tool(annotations=READ)
    async def log_stats() -> Any:
        """What the log holds: files and sizes on disk, counts by level and kind,
        the oldest and newest timestamps, and the rotation/retention settings."""
        return await panel.get("/api/observability/stats", tool="log_stats")

    @server.tool(annotations=READ)
    async def errors_list(
        since: str | None = None,
        limit: int = 50,
        include_traceback: bool = False,
    ) -> Any:
        """Distinct exceptions in the window, grouped by type and raise site.

        Each group carries a `fingerprint`, a count, first/last seen, the loggers
        and request paths involved, and the last request_id. Pass a fingerprint to
        `errors_detail` for the occurrences themselves. Grouping is what keeps a
        log with 4 000 failed rows readable: usually one error, one cause.
        """
        return await panel.get(
            "/api/observability/errors",
            tool="errors_list",
            params={"since": since, "limit": limit, "include_traceback": include_traceback},
        )

    @server.tool(annotations=READ)
    async def errors_detail(fingerprint: str, limit: int = 20) -> Any:
        """Every recorded occurrence of one exception, tracebacks included.

        Take the `fingerprint` from `errors_list`. Tracebacks are capped at 12 000
        characters (the tail is kept) and flagged with `traceback_truncated`.
        """
        return await panel.get(
            f"/api/observability/errors/{quote(fingerprint, safe='')}",
            tool="errors_detail",
            params={"limit": limit},
        )

    @server.tool(annotations=READ)
    async def request_log(
        status_min: int | None = None,
        path: str | None = None,
        limit: int = 100,
    ) -> Any:
        """The request timeline: method, path, status, duration and correlation id.

        Use `status_min=500` for "what has been failing", or `path="/api/workflows"`
        to see one module's traffic. Correlate with `logs_search(request_id=...)`
        to see the log lines that request produced.
        """
        return await panel.get(
            "/api/observability/requests",
            tool="request_log",
            params={"status_min": status_min, "path": path, "limit": limit},
        )
