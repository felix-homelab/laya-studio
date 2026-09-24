"""Request logging and exception capture for the HTTP surface.

Written against the raw ASGI interface rather than Starlette's
``BaseHTTPMiddleware`` on purpose. The MCP endpoint mounted at ``/mcp`` can answer
with ``text/event-stream``, and a middleware that buffers a response to inspect it
would hold that stream open until the response finished — which for SSE is
"never". This one only reads ``http.response.start`` and passes every body chunk
straight through, so streaming is untouched.

It never touches the request body either. Debug logs that archive payloads are a
liability — they turn an evaluation of a customer email into a second copy of that
email on disk — and the interesting parts of a request (path, status, duration,
correlation id, traceback) are all available without it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

from starlette.datastructures import MutableHeaders

from .context import bind, new_request_id, reset
from .records import request_record
from .store import LogStore

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

#: Probes and static assets: high volume, no diagnostic value. The container's own
#: healthcheck hits /api/health every 30s, and an orchestrator's readiness probe
#: hits /api/ready just as often — during a cold start that is a 503 every few
#: seconds, and writing those forever would drown the records that matter. The
#: engine's own lifecycle lines ("loading the checkpoints", "engine ready") are in
#: the store, so "was it ready at 11:54?" is still answerable.
DEFAULT_SKIP_PATHS = ("/static", "/api/health", "/api/ready", "/favicon.ico", "/mcp")

#: Set by the MCP tools on the requests they make (see app/mcp/panel.py). Without
#: it, an agent's call into the API is indistinguishable from the UI's, which is
#: exactly the ambiguity you do not want while debugging "who did this".
MCP_TOOL_HEADER = "x-laya-mcp-tool"

logger = logging.getLogger(__name__)


def _header(scope: Scope, name: str) -> str | None:
    for raw_name, raw_value in scope.get("headers") or ():
        if raw_name.decode("latin-1").lower() == name:
            return raw_value.decode("latin-1")
    return None


class RequestLogMiddleware:
    """One record per request, and a traceback for the ones that raised."""

    def __init__(
        self,
        app: Any,
        store: LogStore,
        *,
        record_requests: bool = True,
        skip_paths: tuple[str, ...] = DEFAULT_SKIP_PATHS,
    ) -> None:
        self.app = app
        self.store = store
        self.record_requests = record_requests
        self.skip_paths = skip_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._interesting(scope):
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        query = (scope.get("query_string") or b"").decode("latin-1")
        client = scope.get("client")
        client_text = f"{client[0]}:{client[1]}" if client else None
        mcp_tool = _header(scope, MCP_TOOL_HEADER)

        token = bind(request_id=request_id, method=method, path=path, client=client_text)
        started = perf_counter()
        status: int | None = None

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 0))
                try:
                    MutableHeaders(scope=message)["X-Request-ID"] = request_id
                except Exception:  # noqa: BLE001 - a header must not fail a response
                    logger.debug("could not attach X-Request-ID", exc_info=True)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            self._write(
                kind="exception",
                method=method,
                path=path,
                query=query,
                client=client_text,
                request_id=request_id,
                status=status,
                duration_ms=(perf_counter() - started) * 1000,
                exc_info=(type(exc), exc, exc.__traceback__),
                mcp_tool=mcp_tool,
            )
            raise
        else:
            if self.record_requests:
                self._write(
                    kind="request",
                    method=method,
                    path=path,
                    query=query,
                    client=client_text,
                    request_id=request_id,
                    status=status,
                    duration_ms=(perf_counter() - started) * 1000,
                    mcp_tool=mcp_tool,
                )
        finally:
            reset(token)

    def _write(self, **kwargs: Any) -> None:
        try:
            self.store.write(request_record(**kwargs))
        except Exception:  # noqa: BLE001 - observability must not fail a response
            logger.debug("could not write request record", exc_info=True)

    def _interesting(self, scope: Scope) -> bool:
        if not self.record_requests and scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            # Still worth recording mutations even when request logging is off.
            return True
        path = scope.get("path", "")
        return not any(path.startswith(prefix) for prefix in self.skip_paths)
