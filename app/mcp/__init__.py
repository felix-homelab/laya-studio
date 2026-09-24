"""The panel's MCP server — its own tool surface, for an agent or a client.

**Why it is here.** Laya Studio is a decision system: the interesting questions
about it ("why did this item escalate", "is the engine even up", "what did that
workflow actually do") are the ones an agent is good at answering *if* it can reach
the panel. Rather than have someone copy a traceback into a chat, the panel
publishes itself over the Model Context Protocol, and the same tools that drive it
also read its logs.

That makes Laya Studio both a decision panel and a debuggable one: an agent can
evaluate a state, watch it fail, read the traceback that failure wrote, find the
schema row behind it, and fix the schema — without leaving the protocol.

Three pieces:

* :mod:`app.mcp.panel` — how a tool reaches the panel (in-process ASGI, or HTTP).
* :mod:`app.mcp.server` — the server, the ``/mcp`` endpoint, and the token gate.
* :mod:`app.mcp.tools` — the tools themselves, one module per API area.

Mounted on the panel at ``/mcp`` by :func:`app.main.create_app`; also runnable
standalone with ``python -m app.mcp`` (or ``laya-studio mcp``) against a panel
somewhere else. See ``docs/observability-and-mcp.md``.
"""

from __future__ import annotations

from .panel import Panel
from .server import (
    DEFAULT_PATH,
    INSTRUCTIONS,
    build_server,
    install,
    lifespan,
    standalone_app,
)

__all__ = [
    "DEFAULT_PATH",
    "INSTRUCTIONS",
    "Panel",
    "build_server",
    "install",
    "lifespan",
    "standalone_app",
]
