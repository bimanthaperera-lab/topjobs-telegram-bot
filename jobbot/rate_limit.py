"""Abuse protection: per-user search limits and per-user / global site-request budgets.

Events are stored in SQLite so limits survive restarts. A limit of 0 disables it.
Cache hits and Show More clicks never count toward the fetch budgets, which
protect the job sites (and the server's IP) from being hammered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .storage import Storage

HOUR = 3600
DAY = 24 * HOUR

KIND_SEARCH = "search"
KIND_FETCH = "fetch"

REASON_COOLDOWN = "cooldown"
REASON_HOURLY = "hourly"
REASON_USER_FETCHES = "user_fetches"
REASON_GLOBAL_FETCHES = "global_fetches"


@dataclass(frozen=True)
class RateLimitConfig:
    cooldown_secs: int = 10
    searches_per_hour: int = 20
    user_fetches_per_day: int = 30
    global_fetches_per_day: int = 500
    admin_user_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None
    retry_after_secs: int = 0

    @classmethod
    def ok(cls) -> "Decision":
        return cls(True)


class RateLimitExceeded(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason)
        self.decision = decision


class RateLimiter:
    def __init__(self, storage: Storage, config: RateLimitConfig) -> None:
        self._storage = storage
        self._config = config

    def is_admin(self, user_id: int) -> bool:
        return user_id in self._config.admin_user_ids

    def _window(self, kind: str, limit: int, window_secs: float, user_id: int | None) -> Decision | None:
        """Sliding-window check; returns a blocking Decision or None."""
        if limit <= 0:
            return None
        now = self._storage.now()
        count, oldest = self._storage.rate_events_since(kind, now - window_secs, user_id)
        if count < limit:
            return None
        retry = (oldest or now) + window_secs - now
        return Decision(False, None, max(1, math.ceil(retry)))

    def check_search(self, user_id: int) -> Decision:
        if self.is_admin(user_id):
            return Decision.ok()
        cooldown = self._config.cooldown_secs
        if cooldown > 0:
            last = self._storage.last_rate_event(KIND_SEARCH, user_id)
            if last is not None:
                wait = last + cooldown - self._storage.now()
                if wait > 0:
                    return Decision(False, REASON_COOLDOWN, max(1, math.ceil(wait)))
        blocked = self._window(KIND_SEARCH, self._config.searches_per_hour, HOUR, user_id)
        if blocked:
            return Decision(False, REASON_HOURLY, blocked.retry_after_secs)
        return Decision.ok()

    def record_search(self, user_id: int) -> None:
        self._storage.add_rate_event(KIND_SEARCH, user_id)

    def check_fetch(self, user_id: int) -> Decision:
        # The global budget protects the sites, so it applies to admins too.
        blocked = self._window(KIND_FETCH, self._config.global_fetches_per_day, DAY, None)
        if blocked:
            return Decision(False, REASON_GLOBAL_FETCHES, blocked.retry_after_secs)
        if self.is_admin(user_id):
            return Decision.ok()
        blocked = self._window(KIND_FETCH, self._config.user_fetches_per_day, DAY, user_id)
        if blocked:
            return Decision(False, REASON_USER_FETCHES, blocked.retry_after_secs)
        return Decision.ok()

    def record_fetch(self, user_id: int) -> None:
        self._storage.add_rate_event(KIND_FETCH, user_id)

    def purge(self) -> int:
        return self._storage.purge_rate_events(DAY)




def format_wait(secs: int) -> str:
    if secs < 60:
        return f"{secs} second{'s' if secs != 1 else ''}"
    minutes = math.ceil(secs / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = math.ceil(minutes / 60)
    return f"{hours} hour{'s' if hours != 1 else ''}"
