"""Tracks whether each job site is working and decides when to alert the admins.

One alert when a site goes down (blocked right away, or after N failures in a
row) and one when it recovers, so a long outage doesn't flood the admin chat.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable

from telegram.error import TelegramError

from .sources import SiteBlockedError, SitePausedError

log = logging.getLogger(__name__)

Notify = Callable[[str], Awaitable[None]]


class SiteHealth:
    def __init__(self, alert_after_failures: int = 3) -> None:
        self._alert_after = max(1, alert_after_failures)
        self._failures: dict[str, int] = {}
        self._down: set[str] = set()

    def is_down(self, site: str) -> bool:
        return site in self._down

    def record_success(self, site: str) -> str | None:
        """Returns a recovery alert if the site had been reported down."""
        self._failures.pop(site, None)
        if site in self._down:
            self._down.discard(site)
            return f"✅ {site} is working again."
        return None

    def record_failure(self, site: str, error: BaseException) -> str | None:
        """Returns an alert the first time a site counts as down, else None."""
        if isinstance(error, SitePausedError):
            return None  # we chose not to ask; already reported when the pause began
        count = self._failures.get(site, 0) + 1
        self._failures[site] = count
        if site in self._down:
            return None
        if isinstance(error, SiteBlockedError):
            self._down.add(site)
            return (
                f"🚫 {site} answered HTTP {error.status} (blocked or rate-limited). "
                f"Requests to it are paused for {round(error.pause_secs / 60)} min; "
                "users get cached results meanwhile."
            )
        if count >= self._alert_after:
            self._down.add(site)
            return (
                f"⚠️ {site} failed {count} times in a row (last error: {error}). "
                "The site may be down or its page layout may have changed."
            )
        return None


class AdminNotifier:
    """Sends plain-text alerts to the admin user IDs once the bot is available."""

    def __init__(self, admin_ids: Iterable[int]) -> None:
        self._admin_ids = sorted(set(admin_ids))
        self._bot = None

    def bind(self, bot) -> None:
        self._bot = bot

    async def __call__(self, text: str) -> None:
        log.warning("Admin alert: %s", text)
        if self._bot is None or not self._admin_ids:
            return
        for admin_id in self._admin_ids:
            try:
                await self._bot.send_message(admin_id, text)
            except TelegramError as exc:
                # Usually the admin hasn't pressed /start with the bot yet.
                log.warning("Could not alert admin %s: %s", admin_id, exc)
