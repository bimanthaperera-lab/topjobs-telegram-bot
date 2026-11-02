"""Configuration loaded from environment variables / a .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .rate_limit import RateLimitConfig, parse_admin_ids


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    apify_api_token: str
    apify_actor_id: str
    apify_memory_mb: int = 256
    apify_fallback_memory_mb: int = 1024
    apify_timeout_secs: int = 120
    cache_ttl_hours: float = 6.0
    apify_fetch_full_details: bool = True
    database_path: str = "jobbot.sqlite3"
    log_level: str = "INFO"
    health_port: int | None = None
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)

    @property
    def cache_ttl_secs(self) -> float:
        return self.cache_ttl_hours * 3600

    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [s for s in (self.telegram_bot_token, self.apify_api_token) if s]


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required setting {name}. Add it to your .env file (see .env.example).")
    return value


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ConfigError(f"{name} must be >= 0, got {value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        apify_api_token=_required("APIFY_API_TOKEN"),
        apify_actor_id=_required("APIFY_ACTOR_ID"),
        apify_memory_mb=_int("APIFY_MEMORY_MB", 256, minimum=128),
        apify_fallback_memory_mb=_int("APIFY_FALLBACK_MEMORY_MB", 1024, minimum=128),
        apify_timeout_secs=_int("APIFY_TIMEOUT_SECS", 120, minimum=10),
        cache_ttl_hours=_float("CACHE_TTL_HOURS", 6.0),
        apify_fetch_full_details=_bool("APIFY_FETCH_FULL_DETAILS", True),
        database_path=os.getenv("DATABASE_PATH", "").strip() or "jobbot.sqlite3",
        log_level=os.getenv("LOG_LEVEL", "").strip().upper() or "INFO",
        # Render (and most PaaS hosts) set PORT; leave unset locally to skip the HTTP endpoint.
        health_port=_int("PORT", 0, minimum=0) or None,
        rate_limit=RateLimitConfig(
            cooldown_secs=_int("RATE_LIMIT_COOLDOWN_SECS", 10, minimum=0),
            searches_per_hour=_int("RATE_LIMIT_SEARCHES_PER_HOUR", 20, minimum=0),
            user_runs_per_day=_int("RATE_LIMIT_USER_RUNS_PER_DAY", 5, minimum=0),
            global_runs_per_day=_int("RATE_LIMIT_GLOBAL_RUNS_PER_DAY", 200, minimum=0),
            admin_user_ids=_admin_ids(),
        ),
    )


def _admin_ids() -> frozenset[int]:
    raw = os.getenv("ADMIN_USER_IDS", "")
    try:
        return parse_admin_ids(raw)
    except ValueError as exc:
        raise ConfigError("ADMIN_USER_IDS must be comma-separated numeric Telegram user IDs") from exc
