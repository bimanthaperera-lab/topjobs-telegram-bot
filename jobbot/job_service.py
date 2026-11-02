"""Search orchestration: shared cache, in-flight run sharing, duplicate filtering, paging."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from .models import Job
from .rate_limit import RateLimiter, RateLimitExceeded
from .storage import Storage

log = logging.getLogger(__name__)

JOBS_PER_SEARCH = 10
PAGE_SIZE = 5
SESSION_MAX_AGE_SECS = 24 * 3600

FetchFn = Callable[[str, int], Awaitable[list[Job]]]


def select_new_jobs(
    jobs: Iterable[Job],
    sent_urls: set[str],
    last_sent_url: str | None,
    *,
    sorted_newest_first: bool,
    limit: int,
) -> list[Job]:
    """Pick up to `limit` jobs the user has not seen.

    When the source is sorted newest first, everything at or after the saved
    most-recent URL is older than what the user already got, so stop there.
    Otherwise early stopping would drop new jobs, so rely on sent_urls only.
    """
    picked: list[Job] = []
    seen: set[str] = set()
    for job in jobs:
        if sorted_newest_first and last_sent_url and job.url == last_sent_url:
            break
        if job.url in sent_urls or job.url in seen:
            continue
        seen.add(job.url)
        picked.append(job)
        if len(picked) >= limit:
            break
    return picked


def paginate(jobs: list[Job], page_size: int = PAGE_SIZE) -> tuple[list[Job], list[Job]]:
    """Split into (first page, the rest)."""
    return jobs[:page_size], jobs[page_size:]


@dataclass(frozen=True)
class SearchResult:
    first_page: list[Job]
    more_session_id: str | None  # set when a Show More page is stored


class JobSearchService:
    def __init__(
        self,
        storage: Storage,
        fetch: FetchFn,
        *,
        cache_ttl_secs: float,
        sorted_newest_first: bool,
        jobs_per_search: int = JOBS_PER_SEARCH,
        page_size: int = PAGE_SIZE,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._limiter = limiter
        self._storage = storage
        self._fetch = fetch
        self._cache_ttl_secs = cache_ttl_secs
        self._sorted_newest_first = sorted_newest_first
        self._jobs_per_search = jobs_per_search
        self._page_size = page_size
        self._in_flight: dict[str, asyncio.Task[list[Job]]] = {}

    async def get_jobs(self, keyword: str, user_id: int | None = None) -> list[Job]:
        """Cached jobs for a keyword; at most one Actor run per keyword at a time.

        Only a search that would start a new Actor run is charged against the run
        budgets; cache hits and joining an in-flight run are free.
        """
        cached = self._storage.get_cached_jobs(keyword, self._cache_ttl_secs)
        if cached is not None:
            log.info("Cache hit for %r (%d jobs)", keyword, len(cached.jobs))
            return cached.jobs

        task = self._in_flight.get(keyword)
        if task is None:
            if self._limiter is not None and user_id is not None:
                decision = self._limiter.check_actor_run(user_id)
                if not decision.allowed:
                    log.warning("Actor run for %r blocked for user %s (%s)", keyword, user_id, decision.reason)
                    raise RateLimitExceeded(decision)
                self._limiter.record_actor_run(user_id)
            log.info("Cache miss for %r; starting Actor run", keyword)
            task = asyncio.create_task(self._fetch_and_cache(keyword))
            self._in_flight[keyword] = task
            task.add_done_callback(lambda _t, k=keyword: self._in_flight.pop(k, None))
        else:
            log.info("Joining in-flight Actor run for %r", keyword)
        # shield: one waiter giving up must not cancel the run others are waiting on
        return await asyncio.shield(task)

    async def _fetch_and_cache(self, keyword: str) -> list[Job]:
        jobs = await self._fetch(keyword, self._jobs_per_search)
        self._storage.save_cached_jobs(keyword, jobs)
        return jobs

    async def search(self, user_id: int, keyword: str) -> SearchResult:
        jobs = await self.get_jobs(keyword, user_id)
        last = self._storage.get_last_sent(user_id, keyword)
        new_jobs = select_new_jobs(
            jobs,
            self._storage.sent_urls(user_id),
            last.url if last else None,
            sorted_newest_first=self._sorted_newest_first,
            limit=self._jobs_per_search,
        )
        first, rest = paginate(new_jobs, self._page_size)
        self._storage.retire_more_sessions(user_id, keyword)
        session_id = self._storage.create_more_session(user_id, keyword, rest) if rest else None
        return SearchResult(first_page=first, more_session_id=session_id)

    def record_sent(self, user_id: int, keyword: str, jobs: list[Job]) -> None:
        if not jobs:
            return
        self._storage.mark_sent(user_id, jobs)
        # Listing order puts the most recent job first.
        self._storage.set_last_sent(user_id, keyword, jobs[0])

    def show_more(self, user_id: int, session_id: str) -> list[Job] | None:
        """Remaining jobs from SQLite only (never runs the Actor). None if expired/used."""
        claimed = self._storage.claim_more_session(session_id, user_id, SESSION_MAX_AGE_SECS)
        if claimed is None:
            return None
        _keyword, jobs = claimed
        sent = self._storage.sent_urls(user_id)
        return [j for j in jobs if j.url not in sent]

    def record_more_sent(self, user_id: int, jobs: list[Job]) -> None:
        if jobs:
            self._storage.mark_sent(user_id, jobs)
