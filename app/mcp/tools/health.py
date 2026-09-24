"""Liveness, readiness and engine state."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..annotations import READ
from ..panel import Panel


def register(server: MCPServer, panel: Panel) -> None:
    @server.tool(annotations=READ)
    async def health() -> Any:
        """Is the panel up, and is Laya usable?

        `status` is "ok" once the engine can serve, "starting" while the checkpoints
        load, "degraded" if they cannot be built at all. `engine.detail` names the
        device, the loaded checkpoints and how long startup took; `database.tables`
        gives row counts per table, which is the quickest way to tell "empty" from
        "broken".
        """
        return await panel.get("/api/health", tool="health")

    @server.tool(annotations=READ)
    async def readiness() -> Any:
        """The readiness gate: 503 until a decision can actually be served.

        Anything that needs the engine returns 503 with a Retry-After until then,
        so a failing `evaluate` is usually this, not a bug. A cold start is on the
        order of a minute with the weights cached and much longer without.
        """
        status, payload = await panel.raw("GET", "/api/ready", tool="readiness")
        return {"http_status": status, "detail": payload}
