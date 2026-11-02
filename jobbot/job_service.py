"""Search orchestration: per-site cache, in-flight sharing, duplicate filtering, paging."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .models import Job
from .rate_limit import RateLimiter, RateLimitExceeded
from .site_health import Notify, SiteHealth
from .sources import JobSource, SourceError
from .storage import Storage

log = logging.getLogger(__name__)

JOBS_PER_SEARCH = 10
PAGE_SIZE = 5
# Cache more than one search needs so repeat searches can still find unseen jobs
# without contacting the site again.
JOBS_PER_SOURCE = 30
SESSION_MAX_AGE_SECS = 24 * 3600
# When a site can't be reached, results up to this old are shown (marked as possibly outdated).
STALE_MAX_SECS = 48 * 3600


class FetchError(RuntimeError):
    """Every site failed and nothing was cached."""


def _group_by_source(jobs: Iterable[Job]) -> dict[str | None, list[Job]]:
    groups: dict[str | None, list[Job]] = {}
    for job in jobs:
        groups.setdefault(job.source, []).append(job)
    return groups


def select_new_jobs(
    jobs: Iterable[Job],
    sent_urls: set[str],
    last_sent_urls: Mapping[str | None, str],
    *,
    newest_first_sources: Container[str | None],
    limit: int,
) -> list[Job]:
    """Pick up to `limit` jobs the user has not seen, alternating between sites.

    For a site whose results are newest first, everything at or after the
    user's saved most-recent URL is older than what they already got, so that
    site's list stops there. Other sites rely on sent_urls only.
    """
    per_source: list[list[Job]] = []
    seen: set[str] = set()
    for source, group in _group_by_source(jobs).items():
        last_url = last_sent_urls.get(source)
        stop_early = source in newest_first_sources and last_url is not None
        picked: list[Job] = []
        for job in group:
            if stop_early and job.url == last_url:
                break
            if job.url in sent_urls or job.url in seen:
                continue
            seen.add(job.url)
            picked.append(job)
            if len(picked) >= limit:
                break
        per_source.append(picked)

    # Round-robin so each site's newest jobs land on the first page.
    merged: list[Job] = []
    index = 0
    while len(merged) < limit and any(index < len(p) for p in per_source):
        for picked in per_source:
            if index < len(picked) and len(merged) < limit:
                merged.append(picked[index])
        index += 1
    return merged


def paginate(jobs: list[Job], page_size: int = PAGE_SIZE) -> tuple[list[Job], list[Job]]:
    """Split into (first page, the rest)."""
    return jobs[:page_size], jobs[page_size:]


@dataclass(frozen=True)
class SearchResult:
    first_page: list[Job]
    more_session_id: str | None  # set when a Show More page is stored
    failed_sources: list[str] = field(default_factory=list)  # nothing to show from these
    stale_sources: list[str] = field(default_factory=list)   # showing older cached results


@dataclass(frozen=True)
class _Collected:
    jobs: list[Job]
    failed_sources: list[str]
    stale_sources: list[str] = field(default_factory=list)


class JobSearchService:
    def __init__(
        self,
        storage: Storage,
        sources: Sequence[JobSource],
        *,
        cache_ttl_secs: float,
        jobs_per_search: int = JOBS_PER_SEARCH,
        page_size: int = PAGE_SIZE,
        jobs_per_source: int = JOBS_PER_SOURCE,
        limiter: RateLimiter | None = None,
        stale_max_secs: float = STALE_MAX_SECS,
        health: SiteHealth | None = None,
        notify: Notify | None = None,
    ) -> None:
        if not sources:
            raise ValueError("at least one job source is required")
        self._stale_max_secs = max(stale_max_secs, cache_ttl_secs)
        self._health = health
        self._notify = notify
        self._alert_tasks: set[asyncio.Task] = set()
        self._storage = storage
        self._sources = list(sources)
        self._cache_ttl_secs = cache_ttl_secs
        self._jobs_per_search = jobs_per_search
        self._page_size = page_size
        self._jobs_per_source = jobs_per_source
        self._limiter = limiter
        self._newest_first = {s.name for s in self._sources if s.sorted_newest_first}
        self._in_flight: dict[str, asyncio.Task[list[Job]]] = {}

    @staticmethod
    def _cache_key(source: JobSource, keyword: str) -> str:
        return f"{source.name}|{keyword}"

    async def get_jobs(self, keyword: str, user_id: int | None = None) -> list[Job]:
        return (await self._collect(keyword, user_id)).jobs

    async def _collect(self, keyword: str, user_id: int | None) -> _Collected:
        """Jobs from every site: cached per site, at most one request per site+keyword at a time.

        Only a search that has to contact a site counts against the fetch
        budgets; cache hits and joining an in-flight request are free.
        """
        cached: dict[str, list[Job]] = {}
        waiting: dict[str, asyncio.Task[list[Job]]] = {}
        to_start: list[JobSource] = []
        for source in self._sources:
            key = self._cache_key(source, keyword)
            hit = self._storage.get_cached_jobs(key, self._cache_ttl_secs)
            if hit is not None:
                cached[source.name] = hit.jobs
            elif key in self._in_flight:
                waiting[source.name] = self._in_flight[key]
            else:
                to_start.append(source)

        # No await between the budget check and starting the requests.
        if to_start and self._limiter is not None and user_id is not None:
            decision = self._limiter.check_fetch(user_id)
            if not decision.allowed:
                has_stale = any(
                    self._storage.get_cached_jobs(self._cache_key(s, keyword), self._stale_max_secs) is not None
                    for s in to_start
                )
                if not cached and not waiting and not has_stale:
                    log.warning("Fetch for %r blocked for user %s (%s)", keyword, user_id, decision.reason)
                    raise RateLimitExceeded(decision)
                to_start = []  # serve fresh or older cached results instead of refusing outright
            else:
                self._limiter.record_fetch(user_id)

        for source in to_start:
            key = self._cache_key(source, keyword)
            log.info("Cache miss for %r on %s; fetching", keyword, source.name)
            task = asyncio.create_task(self._fetch_and_cache(source, key, keyword))
            self._in_flight[key] = task
            task.add_done_callback(lambda _t, k=key: self._in_flight.pop(k, None))
            waiting[source.name] = task

        names = list(waiting)
        # shield: one waiter giving up must not cancel a request others are waiting on
        outcomes = await asyncio.gather(*(asyncio.shield(waiting[n]) for n in names), return_exceptions=True)
        fetched = dict(zip(names, outcomes))

        jobs: list[Job] = []
        failed: list[str] = []
        stale: list[str] = []
        answered = bool(cached)  # some site gave a usable answer, even an empty one
        for source in self._sources:
            if source.name in cached:
                jobs.extend(cached[source.name])
                continue
            outcome = fetched.get(source.name)
            if outcome is not None and not isinstance(outcome, BaseException):
                jobs.extend(outcome)
                answered = True
                continue
            if isinstance(outcome, BaseException):
                # Expected site problems log one line; anything else is a bug and gets a traceback.
                log.error("Search of %s for %r failed: %s", source.name, keyword, outcome,
                          exc_info=None if isinstance(outcome, SourceError) else outcome)
            # Failed, or skipped because the fetch budget is spent: fall back to older results.
            older = self._storage.get_cached_jobs(self._cache_key(source, keyword), self._stale_max_secs)
            if older is not None:
                jobs.extend(older.jobs)
                stale.append(source.name)
            elif outcome is not None:
                failed.append(source.name)
        if failed and not answered and not stale:
            raise FetchError(f"all sources failed: {', '.join(failed)}")
        return _Collected(jobs=jobs, failed_sources=failed, stale_sources=stale)

    async def _fetch_and_cache(self, source: JobSource, key: str, keyword: str) -> list[Job]:
        # Runs once per real request (waiters share the task), so health counts each request once.
        try:
            jobs = await source.fetch(keyword, self._jobs_per_source)
        except Exception as exc:
            self._report(self._health.record_failure(source.name, exc) if self._health else None)
            raise
        log.info("%s returned %d jobs for %r", source.name, len(jobs), keyword)
        self._storage.save_cached_jobs(key, jobs)
        self._report(self._health.record_success(source.name) if self._health else None)
        return jobs

    def set_notifier(self, notify: Notify | None) -> None:
        self._notify = notify

    def _report(self, alert: str | None) -> None:
        if alert is None:
            return
        if self._notify is None:
            log.warning("Site alert (no notifier): %s", alert)
            return
        # Fire and forget so a slow Telegram call never delays the search.
        task = asyncio.create_task(self._notify(alert))
        self._alert_tasks.add(task)
        task.add_done_callback(self._alert_tasks.discard)

    async def search(self, user_id: int, keyword: str) -> SearchResult:
        collected = await self._collect(keyword, user_id)
        last_sent = self._storage.get_last_sent_map(user_id, keyword)
        new_jobs = select_new_jobs(
            collected.jobs,
            self._storage.sent_urls(user_id),
            {source: entry.url for source, entry in last_sent.items()},
            newest_first_sources=self._newest_first,
            limit=self._jobs_per_search,
        )
        first, rest = paginate(new_jobs, self._page_size)
        self._storage.retire_more_sessions(user_id, keyword)
        session_id = self._storage.create_more_session(user_id, keyword, rest) if rest else None
        return SearchResult(
            first_page=first,
            more_session_id=session_id,
            failed_sources=collected.failed_sources,
            stale_sources=collected.stale_sources,
        )

    def record_sent(self, user_id: int, keyword: str, jobs: list[Job]) -> None:
        if not jobs:
            return
        self._storage.mark_sent(user_id, jobs)
        # Each site's list is newest first, so its first job on this page is its most recent.
        for group in _group_by_source(jobs).values():
            self._storage.set_last_sent(user_id, keyword, group[0])

    def show_more(self, user_id: int, session_id: str) -> list[Job] | None:
        """Remaining jobs from SQLite only (never contacts a site). None if expired/used."""
        claimed = self._storage.claim_more_session(session_id, user_id, SESSION_MAX_AGE_SECS)
        if claimed is None:
            return None
        _keyword, jobs = claimed
        sent = self._storage.sent_urls(user_id)
        return [j for j in jobs if j.url not in sent]

    def record_more_sent(self, user_id: int, jobs: list[Job]) -> None:
        if jobs:
            self._storage.mark_sent(user_id, jobs)
