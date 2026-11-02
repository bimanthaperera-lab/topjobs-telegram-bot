"""Post new software engineering jobs to a Telegram channel.

Run on a schedule by GitHub Actions (.github/workflows/job-alerts.yml):

    python -m jobbot.notify

Each run fetches TopJobs.lk's IT-software category (one request), keeps
software engineering titles, and posts the ones not posted before: the ad
image with the job details as its caption and a "View & Apply" button.

Which jobs were already posted is kept in NOTIFY_STATE_FILE (a JSON list of
job URLs), carried between runs by the Actions cache. The first run only
records the current jobs and posts a short "alerts are on" message, so the
channel isn't flooded with every existing opening.

Settings (environment):
    TELEGRAM_BOT_TOKEN   required
    NOTIFY_CHAT_ID       required: "@channelname" or a numeric chat id (-100...)
    NOTIFY_STATE_FILE    default .notify-state/seen.json
    NOTIFY_MAX_PER_RUN   default 15 (Telegram allows ~20 posts/min per channel)
    SEND_JOB_IMAGES      default true
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import AIORateLimiter, ExtBot

from .bot import send_job
from .config import ConfigError, _bool, _int, _required
from .images import JobImages
from .logging_setup import setup_logging
from .models import Job
from .sources.http import PoliteHttpClient, build_http_client
from .sources.topjobs import IT_SOFTWARE_CATEGORY, TopJobsSource
from .storage import Storage

log = logging.getLogger("jobbot.notify")

MAX_REMEMBERED = 3000

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


def is_software_job(title: str) -> bool:
    return bool(SOFTWARE_TITLE.search(title)) and not NOT_SOFTWARE.search(title)


@dataclass(frozen=True)
class NotifySettings:
    bot_token: str
    chat_id: str
    state_file: str
    max_per_run: int
    send_images: bool


def load_notify_settings() -> NotifySettings:
    return NotifySettings(
        bot_token=_required("TELEGRAM_BOT_TOKEN"),
        chat_id=_required("NOTIFY_CHAT_ID"),
        state_file=os.getenv("NOTIFY_STATE_FILE", "").strip() or ".notify-state/seen.json",
        max_per_run=_int("NOTIFY_MAX_PER_RUN", 15),
        send_images=_bool("SEND_JOB_IMAGES", True),
    )


def load_seen(path: str) -> list[str] | None:
    """URLs already posted, oldest first; None on the very first run."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    if not isinstance(data, list):
        raise ValueError(f"{path} is not a JSON list")
    return [u for u in data if isinstance(u, str)]


def save_seen(path: str, urls: list[str]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(urls[-MAX_REMEMBERED:], fh)
    os.replace(tmp, path)  # never leave a half-written file behind


def pick_new(jobs: list[Job], seen: list[str], limit: int) -> tuple[list[Job], list[Job]]:
    """(jobs to post, oldest first so the newest ends up at the bottom of the channel;
    older new jobs beyond the limit, which are skipped)."""
    known = set(seen)
    new = [j for j in jobs if is_software_job(j.title) and j.url not in known]  # newest first
    return list(reversed(new[:limit])), new[limit:]


async def run(settings: NotifySettings, source: TopJobsSource, bot) -> int:
    jobs = await source.fetch("", 500)
    software = [j for j in jobs if is_software_job(j.title)]
    log.info("TopJobs IT category: %d jobs, %d software engineering", len(jobs), len(software))

    seen = load_seen(settings.state_file)
    if seen is None:
        save_seen(settings.state_file, [j.url for j in reversed(software)])
        await bot.send_message(
            settings.chat_id,
            f"✅ Software engineering job alerts are on. Tracking {len(software)} current openings on "
            "TopJobs.lk; new ones will be posted here as they appear.",
        )
        log.info("First run: recorded %d current jobs, posted nothing else", len(software))
        return 0

    to_post, overflow = pick_new(jobs, seen, settings.max_per_run)
    if not to_post:
        log.info("No new software engineering jobs")
        return 0

    total_new = len(to_post) + len(overflow)
    header = f"🆕 <b>{total_new} new software engineering job{'s' if total_new != 1 else ''}</b> on TopJobs.lk"
    if overflow:
        header += f"\n<i>Showing the newest {len(to_post)}.</i>"
    await bot.send_message(settings.chat_id, header, parse_mode=ParseMode.HTML)

    images = JobImages(Storage(":memory:"), [source], enabled=settings.send_images)
    try:
        for job in to_post:
            await send_job(bot, settings.chat_id, job, None, images)
            seen.append(job.url)  # recorded one by one: a failed run never reposts these
        seen.extend(j.url for j in overflow)  # older extras are skipped on purpose
    finally:
        save_seen(settings.state_file, seen)
    log.info("Posted %d new jobs (skipped %d older)", len(to_post), len(overflow))
    return 0


async def main_async() -> int:
    load_dotenv(".env")  # local runs; in GitHub Actions the values come from secrets
    try:
        settings = load_notify_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    setup_logging(os.getenv("LOG_LEVEL", "INFO"), [settings.bot_token])

    http = PoliteHttpClient(build_http_client(20), 2.0)
    source = TopJobsSource(http, category=IT_SOFTWARE_CATEGORY)
    bot = ExtBot(settings.bot_token, rate_limiter=AIORateLimiter(max_retries=3))
    try:
        async with bot:
            return await run(settings, source, bot)
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
