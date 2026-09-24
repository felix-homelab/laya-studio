"""HTTP routers."""

from . import (
    ab,
    analytics,
    bulk,
    health,
    ingest,
    observability,
    pages,
    playground,
    review,
    schemas,
    workflows,
)

#: Registered in this order by :func:`app.main.create_app`.
API_ROUTERS = (
    health.router,
    playground.router,
    schemas.router,
    bulk.router,
    ab.router,
    workflows.router,
    ingest.router,
    analytics.router,
    review.router,
    observability.router,
)

PAGE_ROUTERS = (pages.router,)

__all__ = ["API_ROUTERS", "PAGE_ROUTERS"]
