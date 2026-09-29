"""Jinja2 environment and presentation filters.

The frontend is server-rendered: FastAPI returns HTML shells, Tailwind styles
them from the CDN, and a little vanilla JS hydrates the interactive parts. That
keeps the container small and means the panel runs with nothing but
``pip install -r requirements.txt`` — no Node toolchain.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from .config import get_settings
from .inference.base import BASE_CHECKPOINT_NOTE

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def format_percent(value: Any, digits: int = 1) -> str:
    """``0.828`` -> ``"82.8%"``."""
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def format_number(value: Any, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def format_datetime(value: Any, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc).strftime(fmt)
    return str(value)


def relative_time(value: Any) -> str:
    if not isinstance(value, datetime):
        return format_datetime(value)
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - moment.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds < -60:  # well in the future: show it, rather than hide it as "just now"
        return format_datetime(value)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def to_json(value: Any, indent: int = 2) -> str:
    try:
        return json.dumps(value, indent=indent, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def band_color(band: str) -> str:
    """Tailwind classes for the act-vs-escalate indicator."""
    return {
        "act": "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
        "verify": "bg-amber-500/15 text-amber-300 ring-amber-500/30",
        "escalate": "bg-rose-500/15 text-rose-300 ring-rose-500/30",
    }.get(band, "bg-slate-500/15 text-slate-300 ring-slate-500/30")


def static_url(path: str) -> str:
    """A :file:`/static` URL stamped with the asset's mtime and size.

    There is no build step, so nothing else changes an asset's URL when its
    contents do. Starlette serves ``/static`` with ``ETag``/``Last-Modified`` but
    no ``Cache-Control``, which leaves browsers free to *heuristically* cache a
    script — and then a reload hands back the new HTML shell with the old script
    still attached to it. That pairing is what breaks: the pre-rewrite
    ``playground.js`` looked for ``#questions-json``, a reload brought a shell
    without it, and the page threw before it finished wiring itself up.

    Stamping the URL makes the cache key change whenever the file does, so the
    shell and its scripts are always from the same revision.
    """
    relative = str(path).lstrip("/")
    for prefix in ("static/", "app/static/"):
        if relative.startswith(prefix):
            relative = relative[len(prefix):]
    try:
        info = (STATIC_DIR / relative).stat()
        stamp = f"{info.st_mtime_ns}-{info.st_size}"
    except OSError:  # pragma: no cover - the tag is still served, just unstamped
        stamp = "0"
    return f"/static/{relative}?v={stamp}"


def band_label(band: str) -> str:
    return {
        "act": "Act",
        "verify": "Verify",
        "escalate": "Escalate",
        # ``confidence_band`` returns this when there is no confidence at all.
        "unknown": "No signal",
    }.get(band, "Unknown")


def status_color(status_value: str) -> str:
    return {
        "succeeded": "bg-emerald-500/15 text-emerald-300",
        "ok": "bg-emerald-500/15 text-emerald-300",
        "approved": "bg-emerald-500/15 text-emerald-300",
        "running": "bg-sky-500/15 text-sky-300",
        "in_review": "bg-sky-500/15 text-sky-300",
        "pending": "bg-slate-500/15 text-slate-300",
        "skipped": "bg-slate-500/15 text-slate-300",
        "corrected": "bg-amber-500/15 text-amber-300",
        "failed": "bg-rose-500/15 text-rose-300",
        "rejected": "bg-rose-500/15 text-rose-300",
    }.get(status_value, "bg-slate-500/15 text-slate-300")


#: ``{{ asset('js/playground.js') }}`` — see :func:`static_url`.
templates.env.globals["asset"] = static_url

templates.env.filters["pct"] = format_percent
templates.env.filters["num"] = format_number
templates.env.filters["dt"] = format_datetime
templates.env.filters["ago"] = relative_time
templates.env.filters["json"] = to_json
templates.env.filters["band_color"] = band_color
templates.env.filters["band_label"] = band_label
templates.env.filters["status_color"] = status_color


def page_context(request, **extra: Any) -> dict[str, Any]:
    """Base template context shared by every page.

    Never consults the engine in a way that can raise: pages must render while
    Laya is still downloading, so the operator can see *that* it is downloading.
    """
    from .inference import engine_status

    settings = get_settings()
    status = engine_status()
    engine_info: dict[str, Any] = dict(status.get("detail") or {})
    engine_info.update(
        {
            "name": status.get("name") or "laya",
            "state": status["state"],
            "error": status.get("error"),
            "duration_seconds": status.get("duration_seconds"),
        }
    )
    try:
        from .inference import get_engine

        configuration = getattr(get_engine(), "checkpoint_configuration", None)
        engine_info["configuration"] = configuration() if callable(configuration) else {}
    except Exception:  # noqa: BLE001 - loading or broken; the banner covers both
        logger.debug("checkpoint configuration unavailable", exc_info=True)
        engine_info["configuration"] = {}

    context: dict[str, Any] = {
        "request": request,
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "environment": settings.environment,
        "auto_threshold": settings.auto_threshold,
        "verify_threshold": settings.verify_threshold,
        "llm_provider": settings.llm_provider,
        "llm_enabled": settings.llm_enabled,
        "engine_info": engine_info,
        "engine_state": status["state"],
        "engine_ready": bool(status["ready"]),
        "engine_error": status.get("error"),
        "base_checkpoint_note": BASE_CHECKPOINT_NOTE,
        "ingest_token_required": bool(settings.ingest_token),
        "nav_items": NAV_ITEMS,
    }
    context.update(extra)
    return context


NAV_ITEMS = [
    {"href": "/", "label": "Playground", "icon": "sparkles"},
    {"href": "/schemas", "label": "Schemas", "icon": "squares"},
    {"href": "/bulk", "label": "Bulk testing", "icon": "table"},
    {"href": "/ab", "label": "A/B testing", "icon": "split"},
    {"href": "/workflows", "label": "Workflows", "icon": "bolt"},
    {"href": "/analytics", "label": "Analytics", "icon": "chart"},
    {"href": "/review", "label": "Review queue", "icon": "inbox"},
]
