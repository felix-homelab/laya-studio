"""Runtime configuration for Laya Studio.

Everything is read from the environment (optionally seeded from a ``.env`` file
at the repository root). We deliberately avoid a settings library here so the
configuration surface is explicit and testable: see :func:`get_settings` and
:func:`reload_settings`.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PREFIX = "LAYA_STUDIO_"

VALID_LLM_PROVIDERS = ("none", "openai", "anthropic")


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` reader. Real environment variables always win."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:  # pragma: no cover - unreadable .env should never be fatal
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip one layer of matching quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _str(name: str, default: str = "") -> str:
    return os.environ.get(ENV_PREFIX + name, default).strip()


def _optional_str(name: str) -> str | None:
    value = _str(name)
    return value or None


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    # Unset or unrecognised: a typo must not silently flip a `True` default.
    return default


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _optional_int(name: str) -> int | None:
    raw = _str(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _float(name: str, default: float) -> float:
    raw = _str(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = _str(name)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the effective configuration."""

    # Application
    app_name: str = "Laya Studio"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    secret_key: str = "change-me-in-production"
    cors_origins: list[str] = field(default_factory=list)

    # Storage. PostgreSQL only — see docs/configuration.md.
    database_url: str = "postgresql+psycopg://laya:laya@localhost:5432/laya_studio"
    data_dir: Path = BASE_DIR / "data"
    upload_dir: Path = BASE_DIR / "data" / "uploads"

    # Inference — Laya is the only engine; there is nothing to select.
    laya_preload: bool = True
    laya_device: str = "auto"
    laya_cache_dir: str | None = None
    laya_head_max_len: int | None = None
    laya_max_len: int | None = None
    laya_max_loaded: int | None = None
    laya_token: str | None = None
    laya_offline: bool = False

    # Calibration
    calibration_enabled: bool = True
    calibration_min_samples: int = 30
    calibration_cache_ttl: int = 300

    # Hybrid fallback
    auto_threshold: float = 0.90
    verify_threshold: float = 0.60

    # Generative LLM fallback
    llm_provider: str = "none"
    llm_timeout: float = 30.0
    llm_max_tokens: int = 512
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-3-5-haiku-latest"
    anthropic_base_url: str = "https://api.anthropic.com/v1"

    # Actions
    webhook_timeout: float = 10.0
    webhook_allowed_hosts: list[str] = field(default_factory=list)
    email_enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "laya-studio@localhost"
    smtp_starttls: bool = True

    # Ingestion
    ingest_token: str | None = None
    max_upload_mb: int = 16

    # Observability — the JSONL log/exception store and its read-only API.
    # See docs/observability-and-mcp.md.
    log_jsonl: bool = True
    log_dir: Path = BASE_DIR / "data" / "logs"
    log_max_mb: int = 8
    log_backup_count: int = 5
    log_retention_days: int = 14
    log_requests: bool = True
    observability_token: str | None = None

    # MCP — the panel's own tool surface for agents, mounted at ``/mcp``.
    mcp_enabled: bool = True
    mcp_token: str | None = None
    #: Extra Host headers to accept. Loopback is always allowed; add a container
    #: name or a LAN address when the panel is reached through one.
    mcp_allowed_hosts: list[str] = field(default_factory=list)
    #: Where a *standalone* MCP server (``python -m app.mcp``) finds the panel.
    #: The mounted endpoint ignores this: it is already inside the panel.
    mcp_base_url: str | None = None
    #: Ceiling for one tool call. A bulk job against a cold engine can take minutes,
    #: so this is deliberately not the HTTP default.
    mcp_timeout: float = 300.0

    # -- derived ------------------------------------------------------------

    @property
    def database_is_local(self) -> bool:
        """True when the DSN points at this machine — used only to phrase warnings."""
        # Compare the parsed host, not substrings of the whole DSN; this also
        # handles the bracketed IPv6 form (``@[::1]:5432``).
        try:
            host = urlsplit(self.database_url).hostname
        except ValueError:
            return False
        return host in ("localhost", "127.0.0.1", "::1", "db")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def llm_enabled(self) -> bool:
        """True when a generative fallback provider is configured and keyed."""
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        return False

    def ensure_directories(self) -> None:
        """Create the local scratch directories.

        Uploads are scratch; the log directory is not — it is the one piece of
        state here that is only worth having if it survives a restart, which is why
        docker-compose.yml bind-mounts it out of the container. The database lives
        on a server either way."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        if self.log_jsonl:
            self.log_dir.mkdir(parents=True, exist_ok=True)


def _build_settings() -> Settings:
    _load_dotenv(BASE_DIR / ".env")

    provider = _str("LLM_PROVIDER", "none").lower() or "none"
    if provider not in VALID_LLM_PROVIDERS:
        provider = "none"

    auto_threshold = min(max(_float("AUTO_THRESHOLD", 0.90), 0.0), 1.0)
    verify_threshold = min(max(_float("VERIFY_THRESHOLD", 0.60), 0.0), 1.0)
    # A verify threshold above the auto threshold would make the middle band
    # unreachable; clamp so the ladder always degrades sensibly.
    if verify_threshold > auto_threshold:
        verify_threshold = auto_threshold

    data_dir = BASE_DIR / "data"
    override = _str("DATA_DIR")
    if override:
        data_dir = Path(override).expanduser()
        if not data_dir.is_absolute():
            data_dir = (BASE_DIR / data_dir).resolve()

    # The log directory defaults to a subdirectory of the data directory, so
    # LAYA_STUDIO_DATA_DIR alone is enough to relocate everything a container
    # writes. It gets its own override because it is the one of the two that is
    # bind-mounted back to the host.
    log_dir = data_dir / "logs"
    log_override = _str("LOG_DIR")
    if log_override:
        log_dir = Path(log_override).expanduser()
        if not log_dir.is_absolute():
            log_dir = (BASE_DIR / log_dir).resolve()

    return Settings(
        app_name=_str("APP_NAME", "Laya Studio") or "Laya Studio",
        environment=_str("ENVIRONMENT", "development") or "development",
        log_level=_str("LOG_LEVEL", "INFO").upper() or "INFO",
        secret_key=_str("SECRET_KEY", "change-me-in-production"),
        cors_origins=_list("CORS_ORIGINS"),
        database_url=_str(
            "DATABASE_URL", "postgresql+psycopg://laya:laya@localhost:5432/laya_studio"
        ),
        data_dir=data_dir,
        upload_dir=data_dir / "uploads",
        # Default to preloading: this is a server, and with max_loaded eviction
        # a language switch would otherwise reload a checkpoint mid-request.
        laya_preload=_bool("LAYA_PRELOAD", True),
        laya_device=_str("LAYA_DEVICE", "auto") or "auto",
        laya_cache_dir=_optional_str("LAYA_CACHE_DIR"),
        laya_head_max_len=_optional_int("LAYA_HEAD_MAX_LEN"),
        laya_max_len=_optional_int("LAYA_MAX_LEN"),
        laya_max_loaded=_optional_int("LAYA_MAX_LOADED"),
        # HF_TOKEN is Hugging Face's own variable, so it is read unprefixed.
        laya_token=_optional_str("LAYA_TOKEN") or os.environ.get("HF_TOKEN", "").strip() or None,
        laya_offline=_bool("LAYA_OFFLINE", False),
        calibration_enabled=_bool("CALIBRATION_ENABLED", True),
        calibration_min_samples=_int("CALIBRATION_MIN_SAMPLES", 30),
        calibration_cache_ttl=_int("CALIBRATION_CACHE_TTL", 300),
        auto_threshold=auto_threshold,
        verify_threshold=verify_threshold,
        llm_provider=provider,
        llm_timeout=_float("LLM_TIMEOUT", 30.0),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 512),
        openai_api_key=_optional_str("OPENAI_API_KEY"),
        openai_model=_str("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        openai_base_url=_str("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        anthropic_api_key=_optional_str("ANTHROPIC_API_KEY"),
        anthropic_model=_str("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
        or "claude-3-5-haiku-latest",
        anthropic_base_url=_str("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/"),
        webhook_timeout=_float("WEBHOOK_TIMEOUT", 10.0),
        webhook_allowed_hosts=_list("WEBHOOK_ALLOWED_HOSTS"),
        email_enabled=_bool("EMAIL_ENABLED", False),
        smtp_host=_optional_str("SMTP_HOST"),
        smtp_port=_int("SMTP_PORT", 587),
        smtp_username=_optional_str("SMTP_USERNAME"),
        smtp_password=_optional_str("SMTP_PASSWORD"),
        smtp_from=_str("SMTP_FROM", "laya-studio@localhost") or "laya-studio@localhost",
        smtp_starttls=_bool("SMTP_STARTTLS", True),
        ingest_token=_optional_str("INGEST_TOKEN"),
        max_upload_mb=_int("MAX_UPLOAD_MB", 16),
        log_jsonl=_bool("LOG_JSONL", True),
        log_dir=log_dir,
        log_max_mb=max(1, _int("LOG_MAX_MB", 8)),
        log_backup_count=max(1, _int("LOG_BACKUP_COUNT", 5)),
        log_retention_days=max(0, _int("LOG_RETENTION_DAYS", 14)),
        log_requests=_bool("LOG_REQUESTS", True),
        observability_token=_optional_str("OBSERVABILITY_TOKEN"),
        mcp_enabled=_bool("MCP_ENABLED", True),
        mcp_token=_optional_str("MCP_TOKEN"),
        mcp_allowed_hosts=_list("MCP_ALLOWED_HOSTS"),
        mcp_base_url=_optional_str("MCP_BASE_URL"),
        mcp_timeout=_float("MCP_TIMEOUT", 300.0),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return _build_settings()


def reload_settings() -> Settings:
    """Drop the cached settings and rebuild from the current environment."""
    get_settings.cache_clear()
    return get_settings()


def redact_dsn(url: str) -> str:
    """Hide the password in a DSN before printing or logging it.

    Greedy up to the last ``@``: SQLAlchemy accepts an unescaped ``@`` inside a
    password, and stopping at the first one would leave the rest on display.
    """
    return re.sub(r"://([^:/@]+):.*@", r"://\1:***@", url, count=1)
