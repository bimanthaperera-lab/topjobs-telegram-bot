"""Entry point: python -m jobbot"""

from __future__ import annotations

import logging
import os
import sys
import tempfile

from telegram import Update

from .bot import build_application
from .config import ConfigError, Settings, load_settings
from .images import JobImages
from .job_service import SESSION_MAX_AGE_SECS, JobSearchService
from .logging_setup import setup_logging
from .rate_limit import RateLimiter
from .site_health import AdminNotifier, SiteHealth
from .sources import JobSource
from .sources.http import PoliteHttpClient, build_http_client
from .sources.topjobs import TopJobsSource
from .sources.xpressjobs import XpressJobsSource
from .storage import Storage, StorageError

log = logging.getLogger("jobbot")


def build_sources(settings: Settings, http: PoliteHttpClient) -> list[JobSource]:
    sources: list[JobSource] = []
    if settings.topjobs_enabled:
        sources.append(TopJobsSource(http, category=settings.topjobs_category))
    if settings.xpressjobs_enabled:
        sources.append(XpressJobsSource(http))
    return sources


def open_storage(path: str) -> Storage:
    """Open the database; if its location isn't writable, keep the bot running on a temp file.

    The database only holds caches, history and counters, so losing it on restart
    is acceptable (Render's free plan does that anyway), but crashing is not.
    """
    try:
        return Storage(path)
    except StorageError as exc:
        fallback = os.path.join(tempfile.gettempdir(), "jobbot.sqlite3")
        log.error("%s Falling back to %s; data will not survive restarts.", exc, fallback)
        return Storage(fallback)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.secrets())
    for warning in settings.warnings:
        log.warning("Configuration: %s", warning)

    storage = open_storage(settings.database_path)
    purged = storage.purge_old_sessions(SESSION_MAX_AGE_SECS)
    if purged:
        log.info("Removed %d expired Show More sessions", purged)

    http = PoliteHttpClient(
        build_http_client(settings.http_timeout_secs, settings.scraper_user_agent),
        settings.scrape_min_interval_secs,
        retries=settings.http_retries,
        block_pause_secs=settings.site_backoff_minutes * 60,
    )
    sources = build_sources(settings, http)
    limiter = RateLimiter(storage, settings.rate_limit)
    limiter.purge()
    images = JobImages(storage, sources, enabled=settings.send_job_images)
    notifier = AdminNotifier(settings.rate_limit.admin_user_ids)
    service = JobSearchService(
        storage,
        sources,
        cache_ttl_secs=settings.cache_ttl_secs,
        limiter=limiter,
        stale_max_secs=settings.stale_cache_max_hours * 3600,
        health=SiteHealth(settings.alert_after_failures),
        notify=notifier,
    )

    app = build_application(
        settings.telegram_bot_token, service, limiter, settings.health_port,
        shutdown_hooks=[http.aclose], images=images,
    )
    notifier.bind(app.bot)
    if not settings.rate_limit.admin_user_ids:
        log.warning("ADMIN_USER_IDS is empty; site alerts will only be logged.")
    log.info("Bot starting (sites=%s, TopJobs category=%s, images=%s, cache=%.1fh, min interval=%.1fs)",
             ", ".join(s.name for s in sources), settings.topjobs_category or "all",
             "on" if settings.send_job_images else "off", settings.cache_ttl_hours,
             settings.scrape_min_interval_secs)
    try:
        app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
