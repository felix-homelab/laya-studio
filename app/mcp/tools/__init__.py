"""The tool modules, grouped the way the panel itself is.

One module per API area, each exposing a single ``register(server, panel)``. The
split is not decoration: :mod:`app.mcp.tools.observability` and
:mod:`app.mcp.tools.database` are the two that do not map one-to-one onto a
router, and keeping them separate keeps that visible.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..panel import Panel
from . import (
    ab,
    analytics,
    bulk,
    database,
    health,
    ingest,
    observability,
    playground,
    review,
    schemas,
    workflows,
)

#: Every module that contributes tools, in the order they are registered — the
#: order a `tools/list` response comes back in.
MODULES = (
    health,
    observability,
    schemas,
    playground,
    bulk,
    ab,
    workflows,
    review,
    analytics,
    ingest,
    database,
)


def register_all(server: MCPServer, panel: Panel) -> None:
    """Register every module's tools.

    The count is available from ``await server.list_tools()`` for a caller that
    wants to log it.
    """
    for module in MODULES:
        module.register(server, panel)


__all__ = ["MODULES", "register_all"]
