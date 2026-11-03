"""Configuration loaded from environment variables / a .env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .channel_gate import channel_link
from .rate_limit import RateLimitConfig
from .sources.http import DEFAULT_USER_AGENT


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    topjobs_enabled: bool = True
    # TopJobs category to search within ("SDQ" = IT software); empty = all jobs by keyword.
    topjobs_category: str | None = "SDQ"
    send_job_images: bool = True
    xpressjobs_enabled: bool = False  # needs XpressJobs' permission; see sources/xpressjobs.py
    zenrows_api_key: str | None = None  # send XpressJobs requests through ZenRows
    http_timeout_secs: int = 20
    scrape_min_interval_secs: float = 2.0
    http_retries: int = 2
    site_backoff_minutes: float = 30.0
    stale_cache_max_hours: float = 48.0
    alert_after_failures: int = 3
    scraper_user_agent: str = DEFAULT_USER_AGENT
    cache_ttl_hours: float = 6.0
    database_path: str = "jobbot.sqlite3"
    log_level: str = "INFO"
    health_port: int | None = None
    # Users must join this channel ("@name" or "-100...") before using the bot; None = open.
    required_channel: str | None = None
    required_channel_link: str | None = None  # the Join button's URL
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    # Non-fatal problems found while loading, logged at startup.
    warnings: tuple[str, ...] = ()

    @property
    def cache_ttl_secs(self) -> float:
        return self.cache_ttl_hours * 3600

    def secrets(self) -> list[str]:
        """Values that must never appear in logs."""
        return [s for s in (self.telegram_bot_token, self.zenrows_api_key) if s]


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
    topjobs_enabled = _bool("TOPJOBS_ENABLED", True)
    xpressjobs_enabled = _bool("XPRESSJOBS_ENABLED", False)
    if not (topjobs_enabled or xpressjobs_enabled):
        raise ConfigError("Enable at least one job site: TOPJOBS_ENABLED or XPRESSJOBS_ENABLED.")
    warnings: list[str] = []
    required_channel, required_channel_link = _required_channel()
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        topjobs_enabled=topjobs_enabled,
        topjobs_category=_category(),
        send_job_images=_bool("SEND_JOB_IMAGES", True),
        xpressjobs_enabled=xpressjobs_enabled,
        zenrows_api_key=os.getenv("ZENROWS_API_KEY", "").strip() or None,
        http_timeout_secs=_int("HTTP_TIMEOUT_SECS", 20, minimum=5),
        scrape_min_interval_secs=_float("SCRAPE_MIN_INTERVAL_SECS", 2.0),
        http_retries=_int("HTTP_RETRIES", 2, minimum=0),
        site_backoff_minutes=_float("SITE_BACKOFF_MINUTES", 30.0),
        stale_cache_max_hours=_float("STALE_CACHE_MAX_HOURS", 48.0),
        alert_after_failures=_int("ALERT_AFTER_FAILURES", 3, minimum=1),
        scraper_user_agent=os.getenv("SCRAPER_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
        cache_ttl_hours=_float("CACHE_TTL_HOURS", 6.0),
        database_path=os.getenv("DATABASE_PATH", "").strip() or "jobbot.sqlite3",
        log_level=os.getenv("LOG_LEVEL", "").strip().upper() or "INFO",
        # Render (and most PaaS hosts) set PORT; leave unset locally to skip the HTTP endpoint.
        health_port=_int("PORT", 0, minimum=0) or None,
        required_channel=required_channel,
        required_channel_link=required_channel_link,
        rate_limit=RateLimitConfig(
            cooldown_secs=_int("RATE_LIMIT_COOLDOWN_SECS", 10, minimum=0),
            searches_per_hour=_int("RATE_LIMIT_SEARCHES_PER_HOUR", 20, minimum=0),
            user_fetches_per_day=_int("RATE_LIMIT_USER_FETCHES_PER_DAY", 30, minimum=0),
            global_fetches_per_day=_int("RATE_LIMIT_GLOBAL_FETCHES_PER_DAY", 500, minimum=0),
            admin_user_ids=_admin_ids(warnings),
        ),
        warnings=tuple(warnings),
    )


_BOT_TOKEN_SHAPE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")


def _admin_id_problem(entry: str) -> str:
    """Why an entry isn't a user ID. Never includes the entry itself: it may be a secret."""
    if _BOT_TOKEN_SHAPE.match(entry):
        return "looks like a bot token, not a user ID"
    if entry.startswith("@"):
        return "is a @username; use the numeric ID from @userinfobot instead"
    return "is not a number"


def parse_admin_ids_lenient(raw: str | None) -> tuple[frozenset[int], list[str]]:
    """Parse ADMIN_USER_IDS, keeping valid IDs and describing invalid entries.

    Accepts commas, semicolons or spaces as separators and ignores quotes,
    because values pasted into hosting dashboards often carry them.
    """
    ids: set[int] = set()
    problems: list[str] = []
    entries = [e.strip().strip("\"'[]") for e in re.split(r"[,;\s]+", raw or "")]
    for position, entry in enumerate((e for e in entries if e), start=1):
        if entry.isdigit():
            ids.add(int(entry))
        else:
            problems.append(f"ADMIN_USER_IDS entry {position} {_admin_id_problem(entry)}; it was ignored.")
    return frozenset(ids), problems


def _category() -> str | None:
    raw = os.getenv("TOPJOBS_CATEGORY")
    if raw is None:
        return "SDQ"
    return raw.strip().upper() or None  # set but empty: search all jobs by keyword


_CHANNEL_URL = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$")


def _required_channel() -> tuple[str | None, str | None]:
    channel = os.getenv("REQUIRED_CHANNEL", "").strip()
    if not channel:
        return None, None
    url = _CHANNEL_URL.match(channel)
    if url:
        channel = f"@{url.group(1)}"  # a pasted t.me link means the same public channel
    if not (re.fullmatch(r"@[A-Za-z0-9_]{4,}", channel) or re.fullmatch(r"-100\d+", channel)):
        raise ConfigError("REQUIRED_CHANNEL must be @channelname or a numeric channel id (-100...).")
    link = os.getenv("REQUIRED_CHANNEL_LINK", "").strip() or channel_link(channel)
    if link is None:
        raise ConfigError("REQUIRED_CHANNEL is a numeric (private) channel id, so set REQUIRED_CHANNEL_LINK "
                          "to its invite link (https://t.me/+...).")
    if not link.startswith("https://"):
        raise ConfigError("REQUIRED_CHANNEL_LINK must start with https://")
    return channel, link


def _admin_ids(warnings: list[str]) -> frozenset[int]:
    # Optional setting: a typo here must not stop the bot, so warn instead of failing.
    ids, problems = parse_admin_ids_lenient(os.getenv("ADMIN_USER_IDS", ""))
    warnings.extend(problems)
    return ids
