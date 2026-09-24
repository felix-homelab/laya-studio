"""Observability for Laya Studio: persist what happened, then let a client read it.

Debugging a decision system goes wrong in the same way every time: the interesting
event is a 500 in a workflow run, or an engine that never finished loading, and
the only evidence is a scrollback that has already rotated away. This package is
the answer to that — every log line, every request, and every traceback is written
to a rotated JSONL file, and read back through three doors:

* a shell, with ``grep`` on a file that is bind-mounted out of the container;
* the read-only HTTP surface in :mod:`app.routers.observability`;
* the MCP tools in :mod:`app.mcp`, which is what lets an agent debug the panel
  while it is running.

The store is deliberately dependency-free and local. There is no shipping, no
sampling and no aggregation: three axes of failure that a debugging tool cannot
afford. See :mod:`app.observability.store` for the design and its limits.

Wiring, in one place: :func:`configure` builds the store, attaches the JSONL
handler to the root logger (and to uvicorn's, which does not propagate), captures
``warnings``, and installs the exception hooks. It is called from
:func:`app.logging_conf.configure_logging`, so every entry point — the panel, the
``laya-studio`` CLI, the tests — gets it without remembering to ask.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .context import bind, current, new_request_id, reset
from .handler import JsonlLogHandler, attach_jsonl_handler
from .hooks import install_asyncio_hook, install_exception_hooks, install_warning_capture
from .middleware import RequestLogMiddleware
from .records import (
    LEVELS,
    exception_payload,
    exception_record,
    fingerprint,
    log_record_to_dict,
    normalize_timestamp,
    request_record,
    utc_now,
)
from .store import LogStore, open_store

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Settings

__all__ = [
    "LEVELS",
    "JsonlLogHandler",
    "LogStore",
    "RequestLogMiddleware",
    "attach_jsonl_handler",
    "bind",
    "configure",
    "current",
    "enabled",
    "exception_payload",
    "exception_record",
    "fingerprint",
    "get_store",
    "install_asyncio_hook",
    "install_exception_hooks",
    "log_record_to_dict",
    "new_request_id",
    "normalize_timestamp",
    "open_store",
    "request_record",
    "reset",
    "reset_store",
    "utc_now",
]

logger = logging.getLogger(__name__)

_store: LogStore | None = None
_hooks_installed = False


def configure(settings: Settings | None = None) -> LogStore:
    """Build the store and install the capture hooks. Safe to call repeatedly.

    Every entry point calls this (through ``configure_logging``), and the tests call
    it again with a different data directory, so it must be idempotent: the handler
    and the exception hooks are installed once per process, and re-configuring only
    swaps which file the store writes to.
    """
    global _store, _hooks_installed

    if settings is None:
        from ..config import get_settings

        settings = get_settings()

    store = LogStore(
        settings.log_dir,
        max_bytes=max(1, settings.log_max_mb) * 1024 * 1024,
        backup_count=settings.log_backup_count,
        retention_days=settings.log_retention_days,
    )

    if settings.log_jsonl:
        try:
            store.ensure()
        except OSError:  # pragma: no cover - an unwritable data dir is not fatal
            logger.warning("Cannot create the log directory %s", store.directory, exc_info=True)

    install_warning_capture()
    if not _hooks_installed:
        install_exception_hooks(store)
        _hooks_installed = True

    if settings.log_jsonl:
        attach_jsonl_handler(store)

    _store = store
    return store


def get_store() -> LogStore:
    """The process-wide store, configured on first use."""
    if _store is None:
        return configure()
    return _store


def enabled() -> bool:
    """Whether the JSONL sink is actually writing (as opposed to stderr only)."""
    from ..config import get_settings

    return bool(get_settings().log_jsonl)


def reset_store() -> None:
    """Forget the current store. Tests only — the hooks keep their old reference."""
    global _store
    _store = None
