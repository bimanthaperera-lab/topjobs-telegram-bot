"""Post software engineering jobs to a Telegram channel.

Run on a schedule by GitHub Actions (.github/workflows/job-alerts.yml):

    python -m jobbot.notify

Each run fetches TopJobs.lk's IT-software category (one request) and, when
XPRESSJOBS_ENABLED is on, XpressJobs' newest jobs (up to three requests), keeps
software engineering titles, and posts up to NOTIFY_MAX_PER_RUN (5) of them:

1. New jobs first, each tagged "🆕 New Match": jobs that appeared since alerts
   started and haven't been posted yet (for up to NEW_FOR_HOURS after they
   first appeared).
2. Then the pool: the best NOTIFY_POOL_SIZE (20) jobs by rank, split evenly
   between the sites (10 each with both on), skipping any job posted in the
   last NOTIFY_REPOST_HOURS (24).
3. Nothing qualifies: nothing is posted.

Ranking puts "software engineer"-like titles first (jobbot/relevance.py), then
the rest, newest first. Each job is the ad image with the details as its
caption and a "View & Apply" button; XpressJobs has no ad images, so its jobs
are posted as text with the same button.

What was seen and posted is kept in NOTIFY_STATE_FILE (JSON), carried between
runs by the Actions cache. Jobs already listed when a site is first tracked are
never tagged new, so switching a site on doesn't flood the channel.

Settings (environment):
    TELEGRAM_BOT_TOKEN   required
    NOTIFY_CHAT_ID       required: "@channelname" or a numeric chat id (-100...)
    NOTIFY_STATE_FILE    default .notify-state/seen.json
    NOTIFY_MAX_PER_RUN   default 5
    NOTIFY_POOL_SIZE     default 20 (shared evenly between the sites)
    NOTIFY_REPOST_HOURS  default 24 (e.g. 48 to repeat jobs less often)
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
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, ExtBot

from .bot import send_job
from .config import ConfigError, _bool, _int, _required
from .images import JobImages
from .logging_setup import setup_logging
from .models import Job
from .relevance import rank_jobs
from .sources import JobSource
from .sources.http import PoliteHttpClient, build_http_client
from .sources.topjobs import IT_SOFTWARE_CATEGORY, TopJobsSource
from .sources.xpressjobs import XpressJobsSource
from .storage import Storage

log = logging.getLogger("jobbot.notify")

FETCH_LIMIT = 500  # TopJobs' whole IT category (about 160 jobs) fits in one request
XPRESSJOBS_LATEST = 300  # XpressJobs' newest jobs, all categories: 3 requests, more than a day's postings
NEW_TAG = "🆕 New Match"
NEW_FOR_HOURS = 48  # a job not posted by then loses its "new" priority and joins the ranked pool
FORGET_AFTER_DAYS = 30  # a job no longer listed is forgotten this long after it first appeared
RANK_KEYWORD = "software engineer"

# Titles in the IT category that are software engineering roles...
SOFTWARE_TITLE = re.compile(
    r"\b(software|engineer\w*|developer\w*|programmer\w*|devops|sre|sdet|full[ -]?stack|"
    r"front[ -]?end|back[ -]?end|qa|quality assurance|tech lead|mobile app|web app)\b",
    re.I,
)
# ...minus ones that only share a word with them.
NOT_SOFTWARE = re.compile(
    r"\b(sales|marketing|recruit\w*|accountant|business development|content developer|network|technician)\b",
    re.I,
)
# A site-wide listing (XpressJobs: every category) also has civil, mechanical,
# procurement... engineers, so there a title must name a software role outright.
SOFTWARE_SPECIFIC = re.compile(
    r"\b(software|developer\w*|programmer\w*|devops|sre|sdet|full[ -]?stack|front[ -]?end|back[ -]?end|"
    r"tech lead|mobile app|web app|(qa|quality assurance)\s+(engineer|intern|tester|analyst|automation)\w*|"
    r"(ml|ai|data|cloud|platform|machine learning|test automation|automation|site reliability|ui/ux)"
    r"\s+engineer\w*)\b",
    re.I,
)


def is_software_job(title: str, site_wide: bool = False) -> bool:
    if not SOFTWARE_TITLE.search(title) or NOT_SOFTWARE.search(title):
        return False
    return not site_wide or bool(SOFTWARE_SPECIFIC.search(title))


@dataclass(frozen=True)
class NotifySettings:
    bot_token: str
    chat_id: str
    state_file: str
    max_per_run: int = 5
    send_images: bool = True
    xpressjobs_enabled: bool = False
    pool_size: int = 20
    repost_hours: int = 24


def load_notify_settings() -> NotifySettings:
    return NotifySettings(
        bot_token=_required("TELEGRAM_BOT_TOKEN"),
        chat_id=_required("NOTIFY_CHAT_ID"),
        state_file=os.getenv("NOTIFY_STATE_FILE", "").strip() or ".notify-state/seen.json",
        max_per_run=_int("NOTIFY_MAX_PER_RUN", 5, minimum=1),
        send_images=_bool("SEND_JOB_IMAGES", True),
        xpressjobs_enabled=_bool("XPRESSJOBS_ENABLED", False),
        pool_size=_int("NOTIFY_POOL_SIZE", 20, minimum=1),
        repost_hours=_int("NOTIFY_REPOST_HOURS", 24, minimum=0),
    )


@dataclass
class NotifyState:
    first_seen: dict[str, float] = field(default_factory=dict)  # url -> when it first appeared
    old: set[str] = field(default_factory=set)  # listed when its site was first tracked: never "new"
    posted: dict[str, float] = field(default_factory=dict)  # url -> when it was last posted

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
    if isinstance(data, list):  # the older format: just the URLs already posted
        urls = [u for u in data if isinstance(u, str)]
        return NotifyState(first_seen={u: now for u in urls}, old=set(urls), posted={u: 0.0 for u in urls})
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    old = data.get("old")
    return NotifyState(
        first_seen=_timestamps(data.get("first_seen")),
        old={u for u in old if isinstance(u, str)} if isinstance(old, list) else set(),
        posted=_timestamps(data.get("posted")),
    )


def save_state(path: str, state: NotifyState) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 2, "first_seen": state.first_seen, "old": sorted(state.old),
                   "posted": state.posted}, fh)
    os.replace(tmp, path)  # never leave a half-written file behind


def _site(url: str) -> str:
    return urlsplit(url).netloc.lower()


def _interleave(lists: Sequence[list[Job]]) -> list[Job]:
    """Round-robin, so every site gets its turn near the top."""
    merged: list[Job] = []
    for i in range(max((len(jobs) for jobs in lists), default=0)):
        merged.extend(jobs[i] for jobs in lists if i < len(jobs))
    return merged


def rank_for_channel(jobs: list[Job]) -> list[Job]:
    """Best first: "software engineer"-like titles, then every other job in site order (newest first)."""
    ranked = rank_jobs(jobs, RANK_KEYWORD)
    top = {j.url for j in ranked}
    return ranked + [j for j in jobs if j.url not in top]


def choose(per_site: Sequence[list[Job]], state: NotifyState, now: float,
           settings: NotifySettings) -> list[tuple[Job, bool]]:
    """(job, is_new) to post this run, in posting order.

    New jobs first, best ranked first; then the pool (the top pool_size // sites
    of each site) minus anything posted within repost_hours.
    """
    ranked = [rank_for_channel(jobs) for jobs in per_site]
    new = [j for j in _interleave(ranked) if state.is_new(j.url, now)]
    new_urls = {j.url for j in new}
    share = max(1, settings.pool_size // max(1, len(ranked)))
    pool = _interleave([jobs[:share] for jobs in ranked])
    cutoff = now - settings.repost_hours * 3600
    again = [j for j in pool if j.url not in new_urls and state.posted.get(j.url, float("-inf")) <= cutoff]
    return ([(j, True) for j in new] + [(j, False) for j in again])[: settings.max_per_run]


def _remember(state: NotifyState, per_site: Sequence[tuple[JobSource, list[Job]]], now: float) -> None:
    """Record first sightings, and forget jobs that are gone from the sites that answered."""
    known_sites = {_site(url) for url in state.first_seen}
    listed: set[str] = set()
    answered: set[str] = set()
    for source, software in per_site:
        if not software:
            continue
        host = _site(software[0].url)
        answered.add(host)
        first_time = host not in known_sites  # first run, or a site switched on since
        if first_time:
            log.info("%s: tracking starts with %d current jobs (not tagged new)", source.name, len(software))
        for job in software:
            listed.add(job.url)
            if job.url not in state.first_seen:
                state.first_seen[job.url] = now
                if first_time:
                    state.old.add(job.url)

    forget_before = now - FORGET_AFTER_DAYS * 86400
    for url in list(state.first_seen):
        if url not in listed and _site(url) in answered and state.first_seen[url] < forget_before:
            del state.first_seen[url]
            state.old.discard(url)
            state.posted.pop(url, None)


async def _fetch_software(source: JobSource) -> list[Job]:
    """The site's software engineering jobs, newest first."""
    fetch_latest = getattr(source, "fetch_latest", None)
    if fetch_latest is not None:  # the newest jobs of every category
        jobs = await fetch_latest(XPRESSJOBS_LATEST)
        software = [j for j in jobs if is_software_job(j.title, site_wide=True)]
    else:  # TopJobs: the IT-software category only
        jobs = await source.fetch("", FETCH_LIMIT)
        software = [j for j in jobs if is_software_job(j.title)]
    log.info("%s: %d jobs, %d software engineering", source.name, len(jobs), len(software))
    return software


async def run(settings: NotifySettings, sources: Sequence[JobSource], bot, now: float | None = None) -> int:
    now = time.time() if now is None else now
    per_site: list[tuple[JobSource, list[Job]]] = []
    errors: list[Exception] = []
    for source in sources:
        try:
            software = await _fetch_software(source)
        except Exception as exc:  # one site failing must not stop the others' posts
            log.error("Fetching %s failed: %s", source.name, exc)
            errors.append(exc)
            continue
        per_site.append((source, software))
    if not per_site and errors:
        raise errors[0]
    status = 1 if errors else 0  # a site that failed still marks the run red, after the others post

    state = load_state(settings.state_file, now) or NotifyState()
    _remember(state, per_site, now)
    picks = choose([software for _, software in per_site], state, now, settings)
    if not picks:
        save_state(settings.state_file, state)
        log.info("Nothing to post: no new jobs, and every pool job was posted in the last %dh",
                 settings.repost_hours)
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
    sources: list[JobSource] = [TopJobsSource(http, category=IT_SOFTWARE_CATEGORY)]
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
