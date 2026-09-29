"""Exception capture for everything that does *not* happen inside a request.

The request middleware covers failures that reach an endpoint. These hooks cover
the rest, and they are the ones that matter when the process is misbehaving
rather than a request:

* ``sys.excepthook`` — the main thread dying. A crash in the lifespan (an
  unreachable database, an unmigrated schema) arrives through here.
* ``threading.excepthook`` — a background thread dying. The engine loader runs in
  one, so a torch failure surfaces here and nowhere else.
* the asyncio loop exception handler — a task that failed with nobody awaiting it.
  Installed on the running loop at startup, because there is no global switch.
* ``logging.captureWarnings`` — Python ``Warning``s, which otherwise only show up
  as stderr text with no record of when they happened.

Each hook chains to whatever was installed before, so uvicorn's and the
interpreter's own reporting still happen.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any

from .records import exception_record
from .store import LogStore

logger = logging.getLogger(__name__)


def _record(store: LogStore, exc: BaseException, **context: Any) -> None:
    # Guarded, so every hook still chains to the previous one: a failure to record
    # (an exception whose __str__ raises, say) must not swallow the original report.
    try:
        payload = exception_record(exc, context=context or None)
        store.write(payload)
    except Exception:  # noqa: BLE001 - recording must never mask the original failure
        logger.warning("Could not record %s in the log store", type(exc).__name__, exc_info=True)


def install_exception_hooks(store: LogStore) -> None:
    """Install the interpreter- and thread-level hooks.

    Not idempotent on its own: each call wraps whatever hook is current, so a
    second call records every crash twice. :func:`app.observability.configure`
    installs them once per process.
    """
    previous_excepthook = sys.excepthook

    def excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous_excepthook(exc_type, exc, tb)
            return
        _record(store, exc, source="main-thread")
        previous_excepthook(exc_type, exc, tb)

    sys.excepthook = excepthook

    previous_thread_hook = threading.excepthook

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            _record(
                store,
                args.exc_value,
                source="thread",
                thread=getattr(args.thread, "name", None),
            )
        previous_thread_hook(args)

    threading.excepthook = thread_excepthook


def install_asyncio_hook(loop: asyncio.AbstractEventLoop, store: LogStore) -> None:
    """Record unhandled asyncio task failures.

    Called from the lifespan, which is the first moment a running loop exists.
    The previous handler chains, so the default "Task exception was never
    retrieved" message still reaches stderr.
    """
    previous = loop.get_exception_handler()

    def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            _record(store, exc, source="asyncio", task=str(context.get("task") or "") or None)
        else:
            # A non-exception context (a cancelled task, a bad callback) is still
            # worth a line: it is how "the engine loader quietly never finished"
            # shows up.
            _record(
                store,
                RuntimeError(context.get("message") or "asyncio error"),
                source="asyncio",
                task=str(context.get("task") or "") or None,
            )
        if previous is not None:
            previous(_loop, context)
        else:  # pragma: no cover - the default handler prints to stderr
            logger.error("asyncio error: %s", context.get("message"))

    loop.set_exception_handler(handler)


def install_warning_capture() -> None:
    """Route ``warnings.warn`` into logging, so warnings land in the store too."""
    logging.captureWarnings(True)
