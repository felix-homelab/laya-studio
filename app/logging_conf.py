"""Logging setup."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Settings


def configure_logging(level: str = "INFO", *, settings: Settings | None = None) -> None:
    """Configure root logging once, in a uvicorn-friendly format.

    Logs go to **stderr**, not stdout, so that the CLI's machine-readable output
    (``laya-studio info | jq``) is not corrupted by log lines.

    This is also where the JSONL log store is wired up
    (:func:`app.observability.configure`), because every entry point — the panel's
    lifespan, the ``laya-studio`` CLI, the test suite — already calls this
    function. Anything that logs at all is therefore captured on disk, including
    the startup failures that happen before the app is serving.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(level)

    # Third-party chatter we never want in a panel's logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    from .observability import configure as configure_observability

    configure_observability(settings)
