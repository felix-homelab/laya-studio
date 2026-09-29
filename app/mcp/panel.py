"""How the MCP tools reach the panel.

Two transports, one interface. The difference matters for deployment, not for the
tools:

**In-process (the default).** When the MCP server is mounted on the panel itself
(``/mcp``, see :mod:`app.mcp.server`), every tool call is an ASGI call straight
into the same application. No socket, no port to publish, and the tool runs with
the engine, the database session factory and the settings the panel already has —
including an engine that took two minutes to load, which a second process would
have to load all over again.

**HTTP.** When the server is run standalone (``python -m app.mcp``), or when a
client wants to reach a panel somewhere else, the same calls go out over the
network to ``LAYA_STUDIO_MCP_BASE_URL``.

Either way the tools are talking to the *documented* JSON API rather than to the
service layer directly, and that is deliberate: there is one implementation of
"create a schema" (the route, its validation and its response shape), the panel's
own middleware and request logging see agent traffic exactly as it sees the UI's,
and a tool can never drift from the API in `docs/api-reference.md` without a test
noticing.

The internal token headers are attached here, so a tool never has to care whether
``LAYA_STUDIO_INGEST_TOKEN`` or ``LAYA_STUDIO_OBSERVABILITY_TOKEN`` is set: the
panel authenticates to itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

#: Where a caller does not pass a base_url, and no transport is given.
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: Header carrying the MCP tool name into the panel's request log, so a log line
#: can say "the agent asked for this" instead of looking like a UI request.
TOOL_HEADER = "X-Laya-MCP-Tool"


@dataclass
class Panel:
    """A client for the panel's own JSON API."""

    app: Any = None
    base_url: str | None = None
    timeout: float = 300.0
    ingest_token: str | None = None
    observability_token: str | None = None
    _extra_headers: dict[str, str] = field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def for_app(cls, app: Any, settings: Any) -> Panel:
        """In-process transport: calls go straight into ``app``."""
        return cls(
            app=app,
            timeout=getattr(settings, "mcp_timeout", 300.0),
            ingest_token=getattr(settings, "ingest_token", None),
            observability_token=getattr(settings, "observability_token", None),
        )

    @classmethod
    def for_remote(cls, base_url: str, settings: Any) -> Panel:
        """HTTP transport against a panel that may be on another host."""
        return cls(
            base_url=base_url.rstrip("/") or DEFAULT_BASE_URL,
            timeout=getattr(settings, "mcp_timeout", 300.0),
            ingest_token=getattr(settings, "ingest_token", None),
            observability_token=getattr(settings, "observability_token", None),
        )

    @classmethod
    def from_settings(cls, settings: Any, app: Any = None) -> Panel:
        """In-process when an app is available, otherwise over HTTP."""
        if app is not None:
            return cls.for_app(app, settings)
        return cls.for_remote(getattr(settings, "mcp_base_url", None) or DEFAULT_BASE_URL, settings)

    @property
    def transport(self) -> str:
        return "in-process" if self.app is not None else f"http ({self.base_url})"

    # -- requests -----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        tool: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        """Make one call and return the decoded body.

        A non-2xx response becomes a :class:`ToolError` carrying the panel's own
        ``detail`` — the agent should read the API's message, not a generic failure.
        """
        status, payload = await self.raw(
            method, path, tool=tool, params=params, json=json, data=data, files=files
        )
        if status >= 400:
            raise ToolError(f"{method} {path} -> {status}: {_detail_text(payload)}")
        return payload

    async def raw(
        self,
        method: str,
        path: str,
        *,
        tool: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> tuple[int, Any]:
        """Like :meth:`request`, but hands back the status instead of raising.

        For endpoints where a non-2xx *is* the answer: ``/api/ready`` returns 503
        while the engine is still loading, which is a fact about the panel rather
        than a failed tool call.
        """
        headers = self._headers(path, tool)
        transport = httpx.ASGITransport(app=self.app) if self.app is not None else None
        clean_params = {key: value for key, value in (params or {}).items() if value is not None}

        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url=self.base_url or "http://panel.internal",
                timeout=self.timeout,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    params=clean_params or None,
                    json=json,
                    data=data,
                    files=files,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ToolError(
                f"{method} {path} could not reach the panel over {self.transport}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code == 204 or not response.content:
            return response.status_code, {"ok": True, "status": response.status_code}
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {
                "ok": True,
                "status": response.status_code,
                "body": response.text,
            }

    async def get(self, path: str, *, tool: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, tool=tool, params=params)

    async def get_collection(
        self, path: str, *, tool: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Like :meth:`get`, for endpoints that answer with a JSON array.

        Every tool returns one JSON object, so an array is wrapped as
        ``{"count": n, "items": [...]}``. The SDK expands a returned list into one
        content block *per element*, which means an empty list reaches the client as
        no content at all — a shape that is hard to tell from a failure.
        """
        payload = await self.get(path, tool=tool, params=params)
        if isinstance(payload, list):
            return {"count": len(payload), "items": payload}
        return payload

    async def post(
        self,
        path: str,
        *,
        tool: str,
        json: Any = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        return await self.request("POST", path, tool=tool, json=json, params=params,
                                  data=data, files=files)

    async def put(self, path: str, *, tool: str, json: Any = None) -> Any:
        return await self.request("PUT", path, tool=tool, json=json)

    async def delete(self, path: str, *, tool: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("DELETE", path, tool=tool, params=params)

    # -- internals ----------------------------------------------------------

    def _headers(self, path: str, tool: str) -> dict[str, str]:
        headers = {TOOL_HEADER: tool, **self._extra_headers}
        # The panel authenticating to itself: a tool must work the same whether or
        # not the deployment set these tokens.
        if path.startswith("/api/ingest") and self.ingest_token:
            headers["X-Ingest-Token"] = self.ingest_token
        if path.startswith("/api/observability") and self.observability_token:
            headers["X-Observability-Token"] = self.observability_token
        return headers


def _detail_text(payload: Any) -> str:
    """Pull the useful part out of a decoded error response."""
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, list):
            # FastAPI's request-validation errors: keep locations and messages. The
            # message is under `msg`; the rest of the item echoes the submitted input.
            parts = []
            for item in detail:
                if isinstance(item, dict):
                    location = ".".join(str(bit) for bit in item.get("loc", ()))
                    message = item.get("msg") or item.get("type") or "invalid value"
                    parts.append(f"{location or 'body'}: {message}")
                else:
                    parts.append(str(item))
            return "; ".join(parts) or str(detail)
        return str(detail)
    if isinstance(payload, dict) and "body" in payload:
        return str(payload["body"])[:400] or "no response body"
    return str(payload)[:400] or "no response body"
