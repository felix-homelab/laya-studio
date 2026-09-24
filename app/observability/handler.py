"""The bridge from :mod:`logging` to the JSONL store."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from .records import log_record_to_dict
from .store import LogStore


class JsonlLogHandler(logging.Handler):
    """Write every record to the store as one JSON object.

    Attached to the root logger *and* to uvicorn's own loggers, because uvicorn
    configures its loggers with ``propagate = False``: without the second
    attachment, the traceback uvicorn prints for a request that blew up inside
    Starlette would never reach the store — and that is exactly the line you go
    looking for.
    """

    def __init__(self, store: LogStore, level: int = logging.DEBUG) -> None:
        super().__init__(level=level)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.write(log_record_to_dict(record))
        except Exception:  # noqa: BLE001 - the store must never break logging
            self.handleError(record)


def attach_jsonl_handler(
    store: LogStore,
    *,
    level: int = logging.DEBUG,
    loggers: Iterable[str] = ("", "uvicorn", "uvicorn.error"),
) -> JsonlLogHandler:
    """Attach one handler to each named logger. Idempotent.

    Returns the handler so a caller can detach it again (tests do).
    """
    handler = JsonlLogHandler(store, level=level)
    for name in loggers:
        logger = logging.getLogger(name)
        if any(isinstance(existing, JsonlLogHandler) for existing in logger.handlers):
            continue
        logger.addHandler(handler)
    # uvicorn's access lines duplicate the middleware's request records; they stay
    # on stderr (where uvicorn puts them) but do not need a second copy here.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return handler
