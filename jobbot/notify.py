"""Post IT jobs to a Telegram channel.

Run on a schedule by GitHub Actions (.github/workflows/job-alerts.yml):

    python -m jobbot.notify

Each run fetches TopJobs.lk's two IT categories (software, and hardware/
networks/systems: one request each) and, when XPRESSJOBS_ENABLED is on,
XpressJobs' newest IT-ish jobs (one search per keyword in XPRESSJOBS_KEYWORDS).
It keeps IT titles (software, networking, support, systems, data...) and posts
up to NOTIFY_PER_SITE (5) jobs from each site, so 5 + 5 with both on:

1. New jobs first, each tagged "🆕 New Match": jobs that appeared since alerts
   started and haven't been posted yet (for up to NEW_FOR_HOURS after they
   first appeared).
2. Then the site's pool: its newest jobs (NOTIFY_POOL_SIZE, 20, split evenly
   between the sites: 10 each with both on) that were never posted. With
   NOTIFY_REPOST_HOURS set, a job posted at least that long ago may go again.
3. Nothing qualifies: nothing is posted.

The new jobs of both sites go out first, then the pool jobs. Each job is the ad
image with the details as its caption and a "View & Apply" button; XpressJobs
has no ad images, so its jobs are posted as text with the same button.

What was seen and posted is kept in NOTIFY_STATE_FILE (JSON), carried between
runs by the Actions cache. Jobs already listed when tracking starts, or when
what is tracked changes (a site, a category, the keywords or the title filter),
are never tagged new, so such changes don't flood the channel.

Settings (environment):
    TELEGRAM_BOT_TOKEN   required
    NOTIFY_CHAT_ID       required: "@channelname" or a numeric chat id (-100...)
    NOTIFY_STATE_FILE    default .notify-state/seen.json
    NOTIFY_PER_SITE      default 5 (jobs per site per run)
    NOTIFY_POOL_SIZE     default 20 (shared evenly between the sites)
    NOTIFY_REPOST_HOURS  default unset: a posted job is never posted again
    SEND_JOB_IMAGES      default true
    XPRESSJOBS_ENABLED   default false; only with XpressJobs' permission
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, ExtBot

from .bot import send_job
from .config import ConfigError, _bool, _int, _required
from .images import JobImages
from .logging_setup import setup_logging
from .models import Job
from .sources import JobSource
from .sources.http import PoliteHttpClient, build_http_client
from .sources.topjobs import NAME as TOPJOBS_NAME
from .sources.topjobs import TopJobsSource
from .sources.xpressjobs import XpressJobsSource
from .storage import Storage

log = logging.getLogger("jobbot.notify")

FETCH_LIMIT = 500  # a whole TopJobs category fits in one request
# TopJobs functional areas: IT-Sware/DB/QA/Web/Graphics/GIS, and IT-HWare/Networks/Systems.
TOPJOBS_CATEGORIES = ("SDQ", "HNS")
# XpressJobs lists every category; these searches (newest 100 matches each) find
# its IT jobs. "support", "IT", "technical", "data", "qa" and "engineer" found
# nothing these missed.
XPRESSJOBS_KEYWORDS = ("software", "developer", "devops", "network", "administrator", "troubleshooting",
                       "help desk")
NEW_TAG = "🆕 New Match"
NEW_FOR_HOURS = 48  # a job not posted by then loses its "new" priority and joins the pool
FORGET_AFTER_DAYS = 30  # a job no longer listed is forgotten this long after it first appeared
FILTER_VERSION = "it-2"  # bump when the title filter changes, so newly matching jobs aren't tagged new

# Not IT roles, even in an IT category or with an IT word in the title.
NOT_IT = re.compile(
    r"\b(sales|marketing|recruit\w*|human resources?|accountant|accounting|business development|"
    r"content|copywrit\w*|graphic\w*|video|photograph\w*|client relationship|customer (service|care)|"
    r"data entry)\b",
    re.I,
)
# A site-wide listing (XpressJobs: every category) also has civil engineers,
# security guards and AC technicians, so there a title must name an IT role outright.
IT_ROLE = re.compile(
    r"\b(software|developer\w*|programmer\w*|dev ?ops|devsecops|sre|sdet|full[ -]?stack|front[ -]?end|"
    r"back[ -]?end|tech(nical)? lead|mobile app\w*|web (developer|designer|engineer|master|administrator)\w*|"
    r"network\w*|sys ?admin\w*|systems? (administrator|engineer|analyst|support)\w*|help ?desk|service desk|"
    r"desktop support|technical support|it support|troubleshoot\w*|infrastructure|cloud|cyber\w*|"
    r"information security|security (engineer|analyst|architect|administrator)\w*|"
    r"data (engineer|analyst|scientist|architect)\w*|database\w*|dba|business analyst|"
    r"solutions? architect|ui ?/ ?ux|ux|erp|sap|abap|odoo|salesforce|linux|azure|aws|servers?|"
    r"computer\w*|noc|soc|tester\w*|(qa|quality assurance)\s+(engineer|intern|tester|analyst|automation|lead)\w*|"
    r"(ml|ai|machine learning|test automation|automation|site reliability|platform)\s+engineer\w*|"
    r"(?-i:IT|ICT))\b",
    re.I,
)


def is_it_job(title: str, site_wide: bool = False) -> bool:
    """site_wide=False: the job comes from an IT category, so only non-IT roles are dropped."""
    if NOT_IT.search(title):
        return False
    return not site_wide or bool(IT_ROLE.search(title))


@dataclass(frozen=True)
class NotifySettings:
    bot_token: str
    chat_id: str
    state_file: str
    per_site: int = 5
    send_images: bool = True
    xpressjobs_enabled: bool = False
    pool_size: int = 20
    repost_hours: int | None = None  # None: a posted job is never posted again


def load_notify_settings() -> NotifySettings:
    return NotifySettings(
        bot_token=_required("TELEGRAM_BOT_TOKEN"),
        chat_id=_required("NOTIFY_CHAT_ID"),
        state_file=os.getenv("NOTIFY_STATE_FILE", "").strip() or ".notify-state/seen.json",
        per_site=_int("NOTIFY_PER_SITE", 5, minimum=1),
        send_images=_bool("SEND_JOB_IMAGES", True),
        xpressjobs_enabled=_bool("XPRESSJOBS_ENABLED", False),
        pool_size=_int("NOTIFY_POOL_SIZE", 20, minimum=1),
        repost_hours=(_int("NOTIFY_REPOST_HOURS", 0, minimum=0)
                      if os.getenv("NOTIFY_REPOST_HOURS", "").strip() else None),
    )


@dataclass
class NotifyState:
    first_seen: dict[str, float] = field(default_factory=dict)  # url -> when it first appeared
    old: set[str] = field(default_factory=set)  # listed when tracking (re)started: never "new"
    posted: dict[str, float] = field(default_factory=dict)  # url -> when it was last posted
    scope: str = ""  # what was tracked; a change restarts tracking for unseen jobs

    def is_new(self, url: str, now: float) -> bool:
        return (url not in self.posted and url not in self.old
                and now - self.first_seen.get(url, now) <= NEW_FOR_HOURS * 3600)


def _timestamps(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {k: float(v) for k, v in value.items() if isinstance(k, str) and isinstance(v, (int, float))}


def load_state(path: str, now: float) -> NotifyState | None:
    """The saved state; None on the very first run."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    if isinstance(data, list):  # the oldest format: just the URLs already posted
        urls = [u for u in data if isinstance(u, str)]
        return NotifyState(first_seen={u: now for u in urls}, old=set(urls), posted={u: 0.0 for u in urls})
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    old = data.get("old")
    scope = data.get("scope")
    return NotifyState(
        first_seen=_timestamps(data.get("first_seen")),
        old={u for u in old if isinstance(u, str)} if isinstance(old, list) else set(),
        posted=_timestamps(data.get("posted")),
        scope=scope if isinstance(scope, str) else "",
    )


def save_state(path: str, state: NotifyState) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 3, "scope": state.scope, "first_seen": state.first_seen,
                   "old": sorted(state.old), "posted": state.posted}, fh)
    os.replace(tmp, path)  # never leave a half-written file behind


def tracking_scope(sources: Sequence[JobSource]) -> str:
    """Everything that decides which jobs are tracked."""
    return "|".join([FILTER_VERSION, ",".join(TOPJOBS_CATEGORIES), ",".join(XPRESSJOBS_KEYWORDS),
                     ",".join(sorted(s.name for s in sources))])


def _site(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _interleave(lists: Sequence[list[Job]]) -> list[Job]:
    """Round-robin, so every list gets its turn near the top."""
    merged: list[Job] = []
    for i in range(max((len(jobs) for jobs in lists), default=0)):
        merged.extend(jobs[i] for jobs in lists if i < len(jobs))
    return merged


def choose(per_site: Sequence[list[Job]], state: NotifyState, now: float,
           settings: NotifySettings) -> list[tuple[Job, bool]]:
    """(job, is_new) to post this run, in posting order.

    per_site: each site's jobs, newest first. Each site gets up to
    settings.per_site jobs: its new jobs first, then its pool (its newest
    pool_size // sites) minus anything already posted (or, with repost_hours,
    posted within that many hours). Every site's new jobs are posted before any
    pool job.
    """
    share = max(1, settings.pool_size // max(1, len(per_site)))
    cutoff = None if settings.repost_hours is None else now - settings.repost_hours * 3600

    def can_post(url: str) -> bool:
        last = state.posted.get(url)
        return last is None or (cutoff is not None and last <= cutoff)

    new_picks: list[list[Job]] = []
    pool_picks: list[list[Job]] = []
    for jobs in per_site:
        new = [j for j in jobs if state.is_new(j.url, now)][: settings.per_site]
        new_urls = {j.url for j in new}
        again = [j for j in jobs[:share] if j.url not in new_urls and can_post(j.url)]
        new_picks.append(new)
        pool_picks.append(again[: settings.per_site - len(new)])
    return [(j, True) for j in _interleave(new_picks)] + [(j, False) for j in _interleave(pool_picks)]


def _remember(state: NotifyState, per_site: Sequence[tuple[JobSource, list[Job]]], scope: str,
              now: float) -> None:
    """Record first sightings, and forget jobs that are gone from the sites that answered."""
    restart = state.scope != scope
    if restart and state.first_seen:
        log.info("What is tracked changed: jobs not seen before start out as not new")
    known_sites = {_site(url) for url in state.first_seen}
    listed: set[str] = set()
    answered: set[str] = set()
    for source, jobs in per_site:
        if not jobs:
            continue
        host = _site(jobs[0].url)
        answered.add(host)
        baseline = restart or host not in known_sites  # also covers a site that failed back then
        for job in jobs:
            listed.add(job.url)
            if job.url not in state.first_seen:
                state.first_seen[job.url] = now
                if baseline:
                    state.old.add(job.url)
    state.scope = scope

    forget_before = now - FORGET_AFTER_DAYS * 86400
    for url in list(state.first_seen):
        if url not in listed and _site(url) in answered and state.first_seen[url] < forget_before:
            del state.first_seen[url]
            state.old.discard(url)
            state.posted.pop(url, None)


class TopJobsCategories:
    """TopJobs.lk's IT categories as one site: one request per category."""

    name = TOPJOBS_NAME

    def __init__(self, http: PoliteHttpClient, categories: Sequence[str] = TOPJOBS_CATEGORIES) -> None:
        self._sources = [TopJobsSource(http, category=c) for c in categories]

    async def fetch(self, keyword: str, limit: int) -> list[Job]:
        lists = [await source.fetch(keyword, limit) for source in self._sources]
        return list({j.url: j for j in _interleave(lists)}.values())  # newest of each category first

    async def fetch_image(self, job_url: str) -> bytes | None:
        return await self._sources[0].fetch_image(job_url)


def _unique(jobs: list[Job]) -> list[Job]:
    """The same vacancy is sometimes listed twice ("... | Hybrid" variants): keep the first (newest)."""
    seen: set[tuple[str, str]] = set()
    unique: list[Job] = []
    for job in jobs:
        key = (job.title.split("|")[0].strip().lower(), (job.company or "").strip().lower())
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


async def _fetch_it_jobs(source: JobSource) -> list[Job]:
    """The site's IT jobs, newest first."""
    fetch_newest = getattr(source, "fetch_newest", None)
    if fetch_newest is not None:  # XpressJobs: keyword searches over every category
        jobs = await fetch_newest(XPRESSJOBS_KEYWORDS)
        it_jobs = [j for j in jobs if is_it_job(j.title, site_wide=True)]
    else:  # TopJobs: its IT categories only
        jobs = await source.fetch("", FETCH_LIMIT)
        it_jobs = [j for j in jobs if is_it_job(j.title)]
    it_jobs = _unique(it_jobs)
    log.info("%s: %d jobs, %d IT", source.name, len(jobs), len(it_jobs))
    return it_jobs


async def run(settings: NotifySettings, sources: Sequence[JobSource], bot, now: float | None = None) -> int:
    now = time.time() if now is None else now
    per_site: list[tuple[JobSource, list[Job]]] = []
    errors: list[Exception] = []
    for source in sources:
        try:
            jobs = await _fetch_it_jobs(source)
        except Exception as exc:  # one site failing must not stop the others' posts
            log.error("Fetching %s failed: %s", source.name, exc)
            errors.append(exc)
            continue
        per_site.append((source, jobs))
    if not per_site and errors:
        raise errors[0]
    status = 1 if errors else 0  # a site that failed still marks the run red, after the others post

    state = load_state(settings.state_file, now) or NotifyState()
    _remember(state, per_site, tracking_scope(sources), now)
    picks = choose([jobs for _, jobs in per_site], state, now, settings)
    if not picks:
        save_state(settings.state_file, state)
        log.info("Nothing to post: no new jobs, and the pool's jobs were already posted")
        return status

    images = JobImages(Storage(":memory:"), sources, enabled=settings.send_images)
    try:
        for job, is_new in picks:
            await send_job(bot, settings.chat_id, job, None, images, tag=NEW_TAG if is_new else None)
            state.posted[job.url] = now  # recorded one by one: a failed run never reposts these early
    finally:
        save_state(settings.state_file, state)
    log.info("Posted %d jobs (%d new)", len(picks), sum(is_new for _, is_new in picks))
    return status


async def main_async() -> int:
    load_dotenv(".env")  # local runs; in GitHub Actions the values come from secrets
    try:
        settings = load_notify_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(os.getenv("LOG_LEVEL", "INFO"), [settings.bot_token])

    http = PoliteHttpClient(build_http_client(20), 2.0)
    sources: list[JobSource] = [TopJobsCategories(http)]
    if settings.xpressjobs_enabled:
        sources.append(XpressJobsSource(http))
    bot = ExtBot(settings.bot_token, rate_limiter=AIORateLimiter(max_retries=3))
    try:
        async with bot:
            return await run(settings, sources, bot)
    except TelegramError as exc:
        log.error("Telegram refused the post to %s: %s. Is the bot an admin of the channel "
                  "with permission to post messages?", settings.chat_id, exc)
        return 1
    finally:
        await http.aclose()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
