"""Application factory and lifespan wiring."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings, redact_dsn
from .db import SchemaNotMigrated, get_session_factory, verify_schema
from .inference import EngineNotReady, EngineUnavailable, QuestionTooLarge, start_engine
from .logging_conf import configure_logging
from .mcp import install as install_mcp
from .mcp import lifespan as mcp_lifespan
from .observability import RequestLogMiddleware, get_store, install_asyncio_hook
from .routers import API_ROUTERS, PAGE_ROUTERS
from .services import decision_schemas
from .templating import STATIC_DIR

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_directories()

    logger.info("Starting %s %s (%s)", settings.app_name, settings.app_version, settings.environment)

    # The schema belongs to Prisma's migrations, so the application verifies it
    # rather than creating it. A missing table is a deployment mistake, and it is
    # much better to say so here than to fail with UndefinedTable inside a request.
    try:
        verify_schema()
    except SchemaNotMigrated as exc:
        logger.error("%s", exc)
        raise
    except Exception as exc:  # noqa: BLE001 - connection failures land here
        logger.error(
            "Cannot reach the database at %s: %s: %s\n\n"
            "Laya Studio requires PostgreSQL. Check LAYA_STUDIO_DATABASE_URL and that "
            "the server is running (docs/deployment.md).",
            redact_dsn(settings.database_url),
            type(exc).__name__,
            exc,
        )
        raise
    logger.info("Database ready: %s", redact_dsn(settings.database_url))

    session = get_session_factory()()
    try:
        created = decision_schemas.seed_builtin_templates(session)
        if created:
            logger.info("Seeded %d built-in schema template(s)", len(created))
    finally:
        session.close()

    # Build the engine in the background. Doing it here would block the ASGI
    # lifespan, and uvicorn does not bind its socket until the lifespan returns —
    # so the panel would refuse every connection for the whole download. Instead
    # it is reachable immediately and reports its own engine state until Laya is
    # ready.
    start_engine(background=True)

    if settings.llm_provider != "none" and not settings.llm_enabled:
        logger.warning(
            "LAYA_STUDIO_LLM_PROVIDER=%s but no API key is set; the 60-90%% confidence band "
            "will escalate straight to human review.",
            settings.llm_provider,
        )

    # Now that a loop exists, capture task failures that nobody awaited. The
    # engine loader runs as one; "the model never became ready" is otherwise a
    # silent state with a log line nobody wrote.
    install_asyncio_hook(asyncio.get_running_loop(), get_store())

    # The MCP endpoint's transport only exists while the app is running: the SDK's
    # session manager is single-use, so it is built here and released on shutdown.
    async with mcp_lifespan(app.state, settings):
        yield

    logger.info("Shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "A web panel for Laya typed-decision schemas: an interactive playground, "
            "bulk and A/B evaluation, a hybrid workflow engine, and a calibration monitor."
        ),
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    if settings.cors_origins:
        # A wildcard with credentials would have Starlette echo back any origin
        # as credentialed, so `*` means "any origin, no credentials".
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials="*" not in settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    for router in API_ROUTERS:
        app.include_router(router)
    for router in PAGE_ROUTERS:
        app.include_router(router)

    # Every request gets a correlation id and a record; failures get a traceback in
    # the same store. Added after CORS so it is the outermost layer and can see the
    # final status of a request the CORS layer rejected.
    app.add_middleware(
        RequestLogMiddleware,
        store=get_store(),
        record_requests=settings.log_requests,
    )

    # The panel's own tool surface, at /mcp. Mounted here rather than in a sidecar
    # because it runs in this process: see app/mcp/server.py.
    install_mcp(app, settings)

    @app.exception_handler(EngineUnavailable)
    async def _engine_unavailable_handler(
        _request: Request, exc: EngineUnavailable
    ) -> JSONResponse:
        """The runtime cannot be built at all. Permanent for this process."""
        logger.error("The Laya runtime is unavailable: %s", exc, exc_info=exc)
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "state": "failed", "retry_after": None},
        )

    @app.exception_handler(EngineNotReady)
    async def _engine_not_ready_handler(_request: Request, exc: EngineNotReady) -> JSONResponse:
        """The engine is still loading. Tell the caller when to come back."""
        # INFO, not a warning: on a cold start this is every request for two
        # minutes, and it is the panel working as designed.
        logger.info("Refused a decision: %s (retry in %ss)", exc, exc.retry_after)
        return JSONResponse(
            status_code=503,
            content={
                "detail": str(exc),
                "state": "loading",
                "retry_after": exc.retry_after,
            },
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(QuestionTooLarge)
    async def _too_large_handler(_request: Request, exc: QuestionTooLarge) -> JSONResponse:
        """A schema that Laya cannot physically render. Carry the remedy."""
        logger.warning("A question does not fit the head token budget: %s", exc)
        return JSONResponse(
            status_code=422,
            content={"detail": f"{exc} {exc.remedy}", "remedy": exc.remedy},
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
        """Turn domain validation errors into 422s rather than 500s."""
        logger.info("Rejected a request: %s", exc)
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    return app


app = create_app()
