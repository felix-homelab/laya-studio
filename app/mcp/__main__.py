"""Run the MCP server standalone: ``python -m app.mcp`` / ``laya-studio mcp``.

Useful in three cases the mounted endpoint does not cover:

* an MCP client that speaks **stdio** rather than Streamable HTTP (most desktop
  clients do), where there is no port to mount;
* reaching a panel that is already running somewhere else — a compose stack, a
  staging box — over ``--base-url``;
* exposing a second HTTP endpoint with a different token or path.

    python -m app.mcp                               # stdio, toward localhost:8000
    python -m app.mcp --transport http --port 8765   # a second HTTP endpoint
    python -m app.mcp --base-url https://panel.example.com

Note what standalone mode does *not* do: it does not load the engine. Every tool
call goes over HTTP to the panel's documented API, so the panel does the work and
this process stays a thin client. The one exception is ``db_query``, which needs
its own database connection — set ``LAYA_STUDIO_DATABASE_URL`` for it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from urllib.parse import urlsplit

from ..config import get_settings
from ..logging_conf import configure_logging
from .panel import DEFAULT_BASE_URL, Panel
from .server import DEFAULT_PATH, build_server, standalone_app

logger = logging.getLogger(__name__)

#: Bind addresses that only this machine can reach.
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.mcp",
        description="Serve Laya Studio's tools over the Model Context Protocol.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio (default) for a desktop client, http for a network endpoint.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"The panel to talk to. Defaults to LAYA_STUDIO_MCP_BASE_URL or {DEFAULT_BASE_URL}.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="http transport: bind address.")
    parser.add_argument("--port", type=int, default=8765, help="http transport: bind port.")
    parser.add_argument(
        "--path",
        default=DEFAULT_PATH,
        help=f"http transport: the endpoint path (default {DEFAULT_PATH}).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    base_url = (args.base_url or settings.mcp_base_url or DEFAULT_BASE_URL).rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        parser.error(f"the panel URL must be http(s)://host[:port], got {base_url!r}")
    panel = Panel.for_remote(base_url, settings)
    server = build_server(settings, panel=panel)

    if args.transport == "stdio":
        logger.info("Serving Laya Studio MCP over stdio; panel at %s", base_url)
        server.run("stdio")
        return 0

    import uvicorn

    logger.info(
        "Serving Laya Studio MCP on http://%s:%d%s; panel at %s",
        args.host,
        args.port,
        args.path,
        base_url,
    )
    if not getattr(settings, "mcp_token", None) and args.host not in _LOOPBACK:
        logger.warning(
            "The MCP endpoint on %s:%d is unauthenticated. It can create, run and "
            "delete the panel's data. Set LAYA_STUDIO_MCP_TOKEN to require a bearer "
            "token.",
            args.host,
            args.port,
        )
    uvicorn.run(
        standalone_app(server, settings, args.path),
        host=args.host,
        port=args.port,
        # The root logger's level, already validated by configure_logging:
        # LOG_LEVEL accepts names (WARN, FATAL) that uvicorn's own table does not.
        log_level=logging.getLogger().getEffectiveLevel(),
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin wrapper
    sys.exit(main())
