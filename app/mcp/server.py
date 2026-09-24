"""The panel's MCP server: built here, mounted on the panel at ``/mcp``.

Why the MCP server lives *inside* the panel rather than beside it as a second
process is the whole design, and it comes down to two facts about this system:

* The engine takes about two minutes to load and holds several gigabytes of
  resident weights. A sidecar MCP server that wanted to answer ``evaluate`` would
  either load a second copy or have to talk to the panel over HTTP anyway.
* The panel is already the thing with the database session factory, the settings
  and the log store. Running in-process, a tool call *is* a request to the panel:
  it goes through the same middleware, gets the same correlation id and lands in
  the same log — which is what makes the observability tools honest, because the
  agent's own calls are in the log it is reading.

So the endpoint is mounted onto the FastAPI app and uses the SDK's Streamable HTTP
transport, in **stateless** mode with JSON responses: every POST stands alone, so a
reconnect, a restart or a client that does not echo a session id all work. (The
stateful mode requires the client to send back the ``Mcp-Session-Id`` it was
handed, and a mounted server that keeps session state is one more thing to lose.)

Two details are worth knowing before reading the code:

* **The route is registered directly on the panel's router**, at exactly ``/mcp``.
  Mounting a sub-application instead puts the endpoint at ``/mcp/`` and answers
  ``/mcp`` with a 307, which a POST-ing MCP client may not follow. Attaching the
  route keeps the path exact and skips the sub-app's own middleware — of which
  there is none, unless OAuth is configured, which it is not here.
* **DNS-rebinding protection stays on.** This endpoint has destructive tools and,
  by default, no token, so a page in the user's browser must not be able to reach
  it through a rebound hostname. Loopback is always accepted;
  ``LAYA_STUDIO_MCP_ALLOWED_HOSTS`` adds the container names or LAN addresses a
  deployment reaches the panel through.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..config import get_settings
from .panel import Panel
from .tools import register_all

logger = logging.getLogger(__name__)

#: Where the endpoint lives, on the panel and on the network alike.
DEFAULT_PATH = "/mcp"

#: Request body ceiling. The largest thing a tool sends is a CSV for
#: `bulk_upload`, and this tracks the panel's own MAX_UPLOAD_MB.
MAX_BODY_BYTES = 16 * 1024 * 1024

#: Read by the model when the server is initialised: it is the one place to say
#: what this panel is and how to work with it, before any tool description is read.
INSTRUCTIONS = """\
Laya Studio is a panel around Laya, a non-autoregressive typed-decision model. It
answers typed questions (choice / score / noul) about a text state and returns
calibrated probabilities in one forward pass; it never generates text.

How to use these tools:

* Start with `health` — everything that needs the engine returns 503 while the
  checkpoints are still loading, which is the most common cause of a failing call.
* `evaluate` is the core decision tool: it scores one state against a schema (see
  `schemas_list`) or an inline question bundle.
* When something is wrong, the log store is the fastest path: `logs_tail` for what
  just happened, `errors_list` to see distinct exceptions grouped by raise site,
  `errors_detail` for the tracebacks, and `request_log` for the status/duration
  timeline. Correlate the two with a `request_id`.
* `db_query` runs read-only SQL against the panel's own PostgreSQL when the API
  does not answer the question directly.
* Tools annotated destructive (deletes, workflow runs, ingestion, review retries)
  change real state or have side effects outside the panel. `workflow_run` and
  `ingest` accept `dry_run=True`, which evaluates and returns the route without
  executing any action — prefer it before a real run.
* On terminology: `confidence` is Laya's normalised entropy, not the top
  probability, so a 4-way choice with a 0.94 top probability reports ~0.80. The
  routing ladder gates on it (auto >= 0.90, verify >= 0.60, else human review), and
  Laya Studio's calibration is a second stage fitted on labelled rows.
"""


def build_server(settings: Any = None, app: Any = None, panel: Panel | None = None) -> MCPServer:
    """Build the MCP server and register every tool.

    ``panel`` overrides how the tools reach the panel (used by the standalone
    runner, which targets a base URL rather than an in-process app).
    """
    settings = settings or get_settings()
    panel = panel or Panel.from_settings(settings, app=app)
    server = MCPServer(
        name="laya-studio",
        title="Laya Studio",
        version=settings.app_version,
        instructions=INSTRUCTIONS,
    )
    register_all(server, panel)
    logger.debug("MCP server built over %s", panel.transport)
    return server


def _transport_security(settings: Any) -> TransportSecuritySettings:
    """Hosts and origins the MCP endpoint will answer.

    Loopback is always included: that is where the panel runs, and it is where a
    client on the same machine reaches it. Everything else has to be declared,
    because the alternative is a request from a web page the user did not intend to
    give this much power to.
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "laya-studio:*", "laya-studio"]
    for extra in getattr(settings, "mcp_allowed_hosts", []) or []:
        hosts.extend([extra, f"{extra}:*"])
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    origins.extend(getattr(settings, "cors_origins", []) or [])
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


class MCPEndpoint:
    """Forwards ``/mcp`` to the transport built for the *current* lifespan.

    The SDK's session manager can only be run once per instance — it is built to be
    started by one process lifespan and released at the end. That is fine under
    uvicorn, and it breaks the moment anything enters the lifespan twice: every
    ``TestClient`` in the test suite does, and so does a development reload.

    So the transport is rebuilt on each startup and this indirection keeps the route
    that is registered on the app pointing at whatever is current. It is also what
    makes the endpoint safe to hit before startup: it answers 503 rather than
    reaching for a session manager that does not exist yet.
    """

    def __init__(self) -> None:
        self._endpoint: Any = None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self._endpoint is None:
            response = JSONResponse(
                {"error": "not_ready", "detail": "The MCP transport has not started."},
                status_code=503,
            )
            await response(scope, receive, send)
            return
        await self._endpoint(scope, receive, send)

    def bind(self, endpoint: Any) -> None:
        self._endpoint = endpoint

    def unbind(self) -> None:
        self._endpoint = None


class TokenGate:
    """Reject MCP requests without the configured bearer token.

    An ASGI *class*, not a function, because Starlette treats a plain function
    endpoint as request/response and would call it with a ``Request``. A gate has to
    see ``(scope, receive, send)`` to pass the body through untouched.

    Disabled when ``LAYA_STUDIO_MCP_TOKEN`` is unset, which is the local-panel
    default — the endpoint is then exactly as protected as the rest of the panel,
    which is to say not at all. ``docs/observability-and-mcp.md`` says so plainly.
    """

    def __init__(self, endpoint: Any, token: str | None) -> None:
        self.endpoint = endpoint
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if self.token:
            provided = self._provided(scope)
            if not provided or not hmac.compare_digest(provided, self.token):
                response = JSONResponse(
                    {
                        "error": "unauthorized",
                        "detail": (
                            "Send the token as 'Authorization: Bearer <token>' or "
                            "'X-MCP-Token: <token>'."
                        ),
                    },
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.endpoint(scope, receive, send)

    def _provided(self, scope: dict[str, Any]) -> str | None:
        for raw_name, raw_value in scope.get("headers") or ():
            name = raw_name.decode("latin-1").lower()
            value = raw_value.decode("latin-1")
            if name == "x-mcp-token":
                return value
            if name == "authorization" and value.lower().startswith("bearer "):
                return value[7:].strip()
        return None


def transport(server: MCPServer, settings: Any, path: str = DEFAULT_PATH) -> Starlette:
    """A fresh Streamable HTTP transport — and therefore a fresh session manager.

    Call it once per lifespan: the session manager it returns is single-use (see
    :class:`MCPEndpoint`).
    """
    return server.streamable_http_app(
        streamable_http_path=path,
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(settings),
        max_request_body_size=MAX_BODY_BYTES,
        host="127.0.0.1",
    )


def _endpoint_of(sub: Starlette, path: str) -> Any:
    for route in sub.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise RuntimeError(  # pragma: no cover - the SDK always builds the path
        f"The MCP transport did not build a route for {path!r}."
    )


def standalone_app(server: MCPServer, settings: Any, path: str = DEFAULT_PATH) -> Starlette:
    """An ASGI app serving just the MCP endpoint, for ``python -m app.mcp``."""
    sub = transport(server, settings, path)
    endpoint = TokenGate(_endpoint_of(sub, path), getattr(settings, "mcp_token", None))
    return Starlette(
        routes=[Route(path, endpoint=endpoint)],
        lifespan=lambda _app: server.session_manager.run(),
    )


def install(app: Any, settings: Any = None) -> MCPServer | None:
    """Mount the MCP endpoint on the panel. Returns the server, or None if disabled.

    Called from :func:`app.main.create_app`. The route is appended after the API
    routers, and the transport itself is built by :func:`lifespan` — see
    :class:`MCPEndpoint` for why those are two steps.
    """
    settings = settings or get_settings()
    if not getattr(settings, "mcp_enabled", True):
        logger.info("MCP server disabled (LAYA_STUDIO_MCP_ENABLED=false)")
        return None

    server = build_server(settings, app=app)
    endpoint = MCPEndpoint()
    app.router.routes.append(Route(DEFAULT_PATH, endpoint=endpoint))
    app.state.mcp_server = server
    app.state.mcp_endpoint = endpoint

    if not getattr(settings, "mcp_token", None) and settings.environment != "development":
        logger.warning(
            "The MCP endpoint %s is unauthenticated. It can create, run and delete "
            "the panel's data. Set LAYA_STUDIO_MCP_TOKEN to require a bearer token, "
            "or LAYA_STUDIO_MCP_ENABLED=false to turn it off.",
            DEFAULT_PATH,
        )
    return server


@asynccontextmanager
async def lifespan(app_state: Any, settings: Any) -> AsyncIterator[None]:
    """Run the MCP transport for the duration of the application's lifespan.

    Does nothing when the endpoint is not mounted (:func:`install` was skipped), so
    the panel's own lifespan does not have to branch.
    """
    server = getattr(app_state, "mcp_server", None)
    endpoint: MCPEndpoint | None = getattr(app_state, "mcp_endpoint", None)
    if server is None or endpoint is None:
        yield
        return

    sub = transport(server, settings)
    endpoint.bind(TokenGate(_endpoint_of(sub, sub.routes[0].path), settings.mcp_token))
    try:
        tools = await server.list_tools()
        logger.info(
            "MCP endpoint ready at %s (%d tools%s)",
            DEFAULT_PATH,
            len(tools),
            "" if getattr(settings, "mcp_token", None) else ", unauthenticated",
        )
        async with server.session_manager.run():
            yield
    finally:
        endpoint.unbind()
