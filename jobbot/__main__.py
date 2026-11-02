"""Entry point: python -m jobbot"""

from __future__ import annotations

import logging
import sys

from apify_client import ApifyClientAsync
from telegram import Update

from . import actor_mapping
from .apify_service import ApifyJobFetcher
from .bot import build_application
from .config import ConfigError, load_settings
from .job_service import SESSION_MAX_AGE_SECS, JobSearchService
from .logging_setup import setup_logging
from .rate_limit import RateLimiter
from .storage import Storage

log = logging.getLogger("jobbot")


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(settings.log_level, settings.secrets())

    storage = Storage(settings.database_path)
    purged = storage.purge_old_sessions(SESSION_MAX_AGE_SECS)
    if purged:
        log.info("Removed %d expired Show More sessions", purged)

    fetcher = ApifyJobFetcher(
        ApifyClientAsync(token=settings.apify_api_token),
        settings.apify_actor_id,
        memory_mb=settings.apify_memory_mb,
        fallback_memory_mb=settings.apify_fallback_memory_mb,
        timeout_secs=settings.apify_timeout_secs,
        fetch_full_details=settings.apify_fetch_full_details,
    )
    limiter = RateLimiter(storage, settings.rate_limit)
    limiter.purge()
    service = JobSearchService(
        storage,
        fetcher.fetch,
        cache_ttl_secs=settings.cache_ttl_secs,
        sorted_newest_first=actor_mapping.ACTOR_SORTS_NEWEST,
        limiter=limiter,
    )

    app = build_application(settings.telegram_bot_token, service, limiter, settings.health_port)
    log.info("Bot starting (actor=%s, memory=%dMB, timeout=%ds, cache=%.1fh)",
             settings.apify_actor_id, settings.apify_memory_mb,
             settings.apify_timeout_secs, settings.cache_ttl_hours)
    try:
        app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
