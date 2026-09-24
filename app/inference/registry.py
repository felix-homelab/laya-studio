"""Engine lifecycle.

Laya is the engine. There is no fallback, no stand-in, and no configuration value
that selects something else.

**Why construction is asynchronous.** Building the engine can take minutes: on a
cold cache it downloads ~800 MB of checkpoints before it can load them. Uvicorn
binds its listening socket only *after* the ASGI lifespan startup returns
(verified), so doing this work inline would mean the panel refuses every
connection — `ECONNREFUSED`, not a slow page — for the whole download. Instead the
lifespan kicks off a background thread and the panel is immediately reachable,
reporting its own engine state. Endpoints that need Laya return 503 with a
`Retry-After` until it is ready.

States:

``pending``   nothing started yet
``loading``   a thread is downloading/loading checkpoints
``ready``     the engine is usable
``failed``    the runtime is broken; retrying will not help
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter

from ..config import Settings, get_settings
from .base import DecisionEngine, EngineNotReady, EngineUnavailable
from .laya_engine import LayaEngine

logger = logging.getLogger(__name__)

PENDING = "pending"
LOADING = "loading"
READY = "ready"
FAILED = "failed"

#: How long a caller should wait before retrying while the engine loads.
DEFAULT_RETRY_AFTER = 10


@dataclass
class AsyncEngineState:
    """Thread-safe-ish snapshot of the background build."""

    state: str = PENDING
    error: str | None = None
    started_at: datetime | None = None
    ready_at: datetime | None = None
    duration_seconds: float | None = None

    @property
    def ready(self) -> bool:
        return self.state == READY


_lock = threading.RLock()
_ready_event = threading.Event()
_state = AsyncEngineState()
_engine: DecisionEngine | None = None
_build_thread: threading.Thread | None = None

#: Test-only override. Never set from configuration or from HTTP.
_override: DecisionEngine | None = None


def _build(settings: Settings) -> None:
    global _engine
    started = perf_counter()
    try:
        engine = build_engine(settings)
    except EngineUnavailable as exc:
        message = str(exc)
        logger.error("Laya engine could not be built: %s", message)
        with _lock:
            _state.state = FAILED
            _state.error = message
            _state.duration_seconds = perf_counter() - started
        _ready_event.set()
        return
    except Exception as exc:  # noqa: BLE001 - never let a build kill the process
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("Laya engine failed unexpectedly")
        with _lock:
            _state.state = FAILED
            _state.error = message
            _state.duration_seconds = perf_counter() - started
        _ready_event.set()
        return

    with _lock:
        _engine = engine
        _state.state = READY
        _state.ready_at = datetime.now(timezone.utc)
        _state.duration_seconds = perf_counter() - started
    _ready_event.set()
    logger.info("Laya engine ready in %.1fs: %s", _state.duration_seconds, engine.describe())


def start_engine(*, background: bool = True, settings: Settings | None = None) -> None:
    """Begin building the engine. Idempotent.

    ``background=True`` (the default, and what the app lifespan uses) returns
    immediately. ``background=False`` blocks until the build finishes — what the
    CLI wants.
    """
    global _build_thread

    if _override is not None:
        _ready_event.set()
        return

    settings = settings or get_settings()

    with _lock:
        if _state.state in (LOADING, READY):
            already_started = True
        elif _state.state == FAILED:
            already_started = True
        else:
            already_started = False
            _state.state = LOADING
            _state.started_at = datetime.now(timezone.utc)
            _state.error = None
            _ready_event.clear()

    if already_started:
        if not background:
            _ready_event.wait()
        return

    logger.info(
        "Loading the Laya checkpoints in the background. The panel is reachable now; "
        "evaluations will return 503 until this finishes. A cold cache downloads "
        "roughly 800 MB, so this can take several minutes."
    )

    if background:
        _build_thread = threading.Thread(
            target=_build, args=(settings,), name="laya-engine-build", daemon=True
        )
        _build_thread.start()
    else:
        _build(settings)


def wait_for_engine(timeout: float | None = None) -> bool:
    """Block until the build settles. Returns whether the engine is *usable*.

    The readiness event fires when the build finishes, successfully or not — so the
    return value is the state, not the event. Returning the event would report a
    permanently broken runtime as ready.
    """
    if _override is not None:
        return True
    if _state.state == PENDING:
        start_engine(background=True)
    _ready_event.wait(timeout=timeout)
    return _state.state == READY


def build_engine(settings: Settings | None = None) -> LayaEngine:
    """Construct a :class:`LayaEngine`. Raises if the runtime is unusable."""
    settings = settings or get_settings()
    return LayaEngine(
        preload=settings.laya_preload,
        device=settings.laya_device,
        cache_dir=settings.laya_cache_dir,
        head_max_len=settings.laya_head_max_len,
        max_len=settings.laya_max_len,
        max_loaded=settings.laya_max_loaded,
        token=settings.laya_token,
        offline=settings.laya_offline,
    )


def get_engine() -> DecisionEngine:
    """Return the engine, or raise. Never blocks.

    ``EngineNotReady`` while the checkpoints are still loading;
    ``EngineUnavailable`` if the runtime is broken.
    """
    if _override is not None:
        return _override

    with _lock:
        state = _state.state
        engine = _engine
        error = _state.error

    if state == READY and engine is not None:
        return engine

    if state == FAILED:
        raise EngineUnavailable(
            f"{error}\n\n"
            "Laya Studio has no fallback engine: every decision must come from Laya.\n"
            "Run `laya-studio check` to diagnose the environment."
        )

    if state == PENDING:
        raise EngineNotReady(
            "The Laya runtime has not been started in this process.",
            retry_after=DEFAULT_RETRY_AFTER,
        )

    raise EngineNotReady(
        "Laya is still loading — the first start downloads the checkpoints, which "
        "can take several minutes. The rest of the panel is available.",
        retry_after=DEFAULT_RETRY_AFTER,
    )


def require_engine(timeout: float | None = None) -> DecisionEngine:
    """Block until the engine is ready, then return it.

    Used by the CLI and by scripts, where waiting is the right thing. The web app
    does *not* use this at startup — see the module docstring.
    """
    start_engine(background=True)
    if timeout is not None:
        _ready_event.wait(timeout=timeout)
    else:
        _ready_event.wait()
    return get_engine()


def engine_status() -> dict:
    """Diagnostic payload that never raises. Backs ``/api/health`` and the UI banner."""
    if _override is not None:
        detail = _override.describe()
        return {
            "state": READY,
            "ready": True,
            "error": None,
            "detail": detail,
            "name": detail.get("name", "unknown"),
            "device": detail.get("device"),
            "loaded_checkpoints": detail.get("loaded_checkpoints", []),
            "duration_seconds": None,
        }

    with _lock:
        state = _state.state
        error = _state.error
        duration = _state.duration_seconds
        started_at = _state.started_at
        engine = _engine

    detail: dict = {}
    if state == READY and engine is not None:
        detail = engine.describe()

    return {
        "state": state,
        "ready": state == READY,
        "error": error,
        "detail": detail,
        "name": detail.get("name", "laya" if state != FAILED else "unavailable"),
        "device": detail.get("device"),
        "loaded_checkpoints": detail.get("loaded_checkpoints", []),
        "started_at": None if started_at is None else started_at.isoformat(),
        "duration_seconds": None if duration is None else round(duration, 1),
    }


def set_engine(engine: DecisionEngine | None) -> None:
    """Install a test double, or clear it with ``None``.

    The *only* substitution point, and test-only. Nothing in the application, the
    configuration, or the HTTP API calls it.
    """
    global _override
    _override = engine


def reset_engine_cache() -> None:
    """Forget the engine and its state, so the next call rebuilds."""
    global _engine, _build_thread, _state
    with _lock:
        _engine = None
        _build_thread = None
        _state = AsyncEngineState()
    _ready_event.clear()
