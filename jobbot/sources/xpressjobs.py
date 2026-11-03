"""XpressJobs: the JSON API behind https://xpress.jobs.

Search: GET /api/jobs/searchJobs?page=1&pageSize=N&keyword=<kw>&sortBy=ExpiryDateOnWebsite DESC
returns a JSON array of jobs. The public sort options are by expiry date only,
so results are re-sorted by jobId (assigned in posting order) to get newest first.
The channel alerts (fetch_newest) instead ask for sortBy=JobId DESC, which
the API accepts: the newest matches first.

XpressJobs' terms of use forbid scraping without permission, so this source is
disabled by default (XPRESSJOBS_ENABLED=false). Enable it only once XpressJobs
has given written permission.

XpressJobs blocks GitHub Actions' IP addresses and asked for requests to go
through ZenRows instead: with ZENROWS_API_KEY set, every request is sent to the
ZenRows API (GET https://api.zenrows.com/v1/?apikey=...&url=<the API URL>),
which fetches the URL and returns XpressJobs' answer unchanged. When ZenRows
refuses (402: out of credits, 429: too many requests), ZenRowsLimitError is
raised: XpressJobs is skipped and the bot and channel carry on with TopJobs.

Some jobs have an ad image: /api/vacancy/getJobDescriptionImage?jobId=<id>
answers with the image, or 204 (no content) when the job has none. Its URL is
passed to Telegram, which downloads it itself (no request from here, so no
ZenRows credit and no blocked IP); the company logo is the fallback.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ..models import Job
from . import SiteBlockedError, SitePausedError, SourceError
from .http import PoliteHttpClient

log = logging.getLogger(__name__)

NAME = "XpressJobs"
SEARCH_URL = "https://xpress.jobs/api/jobs/searchJobs"
JOB_URL = "https://xpress.jobs/jobs/view/{job_id}"
AD_IMAGE_URL = "https://xpress.jobs/api/vacancy/getJobDescriptionImage?jobId={job_id}"
SORT_BY = "ExpiryDateOnWebsite DESC"
MAX_PAGE_SIZE = 100  # one modest request; the API filters by keyword itself
LATEST_SORT_BY = "JobId DESC"  # not offered in the site's UI, but the API accepts it: newest first
ZENROWS_URL = "https://api.zenrows.com/v1/"
# The API takes 4-20s per search, and ZenRows adds its own time on top: 20s timed out.
ZENROWS_TIMEOUT_SECS = 60
# fetch_newest gives up on the remaining keywords after this many fail, so a slow
# day can't run past the alerts job's time limit (each failure is up to 3 timeouts).
MAX_FAILED_KEYWORDS = 2
# The bot's searches use one list of IT jobs, made from these searches and kept fresh
# in the background, because the API takes 4-20s per search. They cover the bot's
# categories (see categories.py).
IT_KEYWORDS = ("software", "developer", "engineer", "qa", "devops", "cloud", "designer", "data", "security",
               "support", "help desk", "network", "administrator", "project manager", "intern", "trainee")

_JOB_ID = re.compile(r"^https://xpress\.jobs/jobs/view/(\d+)$")
_BOILERPLATE = re.compile(r"\s*please refer (to )?the job advert(isement)? for (further|more) (information|details)\.?", re.I)


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value or None


def _date(value: Any) -> str | None:
    """'2026-11-10T00:00:00' -> 'Tue Nov 10 2026' (same style as TopJobs)."""
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).strftime("%a %b %d %Y")
    except ValueError:
        return text


def _description(item: dict) -> str | None:
    overview = _clean(item.get("overview"))
    if overview:
        overview = _BOILERPLATE.sub("", overview).strip() or None
    job_type = _clean(item.get("jobType"))
    if overview and job_type:
        return f"{job_type}. {overview}"
    return overview or job_type


def _logo(item: dict) -> str | None:
    url = _clean(item.get("logoImageUrl")) if item.get("hasLogo") else None
    return url if url and url.lower().startswith("https://") else None


def map_job(item: Any) -> Job | None:
    if not isinstance(item, dict) or item.get("archived"):
        return None
    job_id = item.get("jobId")
    title = _clean(item.get("jobTitle"))
    if not isinstance(job_id, int) or not title:
        return None
    return Job(
        url=JOB_URL.format(job_id=job_id),
        title=title,
        company=_clean(item.get("organizationName")),
        location=_clean(item.get("locations")),
        posted=_date(item.get("createdDate")),
        closing=_date(item.get("expiryDateOnWebsite")),
        description=_description(item),
        source=NAME,
        logo_url=_logo(item),
    )


def parse_search_response(data: Any) -> list[Job]:
    """Map the API response to Jobs, newest (highest jobId) first."""
    if not isinstance(data, list):
        raise SourceError(f"{NAME}: unexpected response shape")
    pairs = [(item.get("jobId"), map_job(item)) for item in data if isinstance(item, dict)]
    pairs = [(job_id, job) for job_id, job in pairs if job is not None]
    pairs.sort(key=lambda p: p[0], reverse=True)
    seen: set[str] = set()
    jobs: list[Job] = []
    for _, job in pairs:
        if job.url not in seen:
            seen.add(job.url)
            jobs.append(job)
    return jobs


class ZenRowsLimitError(SourceError):
    """ZenRows refused the request (out of credits or too many requests): skip XpressJobs for now."""


class XpressJobsSource:
    name = NAME
    sorted_newest_first = True

    def __init__(self, http: PoliteHttpClient, zenrows_api_key: str | None = None,
                 pool_keywords: Sequence[str] = ()) -> None:
        """pool_keywords: fetch() returns the newest jobs for these, whatever the keyword, so one
        list serves every search; the search service refreshes it in the background."""
        self._http = http
        self._zenrows_api_key = zenrows_api_key or None
        self._pool_keywords = tuple(pool_keywords)
        self.keyword_independent = self.refreshed_in_background = bool(self._pool_keywords)

    async def _search(self, page: int, page_size: int, keyword: str, sort_by: str) -> list:
        params = {"page": page, "pageSize": page_size, "keyword": keyword, "sortBy": sort_by}
        if self._zenrows_api_key:
            target = f"{SEARCH_URL}?{urlencode(params)}"
            try:
                response = await self._http.get(ZENROWS_URL, params={"apikey": self._zenrows_api_key, "url": target},
                                                timeout=ZENROWS_TIMEOUT_SECS)
            except (SiteBlockedError, SitePausedError) as exc:
                raise ZenRowsLimitError(f"{NAME}: ZenRows limit reached ({exc})") from exc
        else:
            response = await self._http.get(SEARCH_URL, params=params, headers={"Accept": "application/json"})
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"{NAME}: response was not JSON") from exc
        if not isinstance(data, list):
            raise SourceError(f"{NAME}: unexpected response shape")
        return data

    async def fetch(self, keyword: str, limit: int) -> list[Job]:
        if self._pool_keywords:
            return await self.fetch_newest(self._pool_keywords)  # the whole pool: searches pick from it
        # The site's own frontend doubles single quotes before sending.
        data = await self._search(1, min(limit, MAX_PAGE_SIZE), keyword.replace("'", "''"), SORT_BY)
        return parse_search_response(data)[:limit]

    async def ad_image_url(self, job_url: str) -> str | None:
        """Where the job's ad image would be (no request: Telegram finds out if there is one)."""
        match = _JOB_ID.match(job_url)
        return AD_IMAGE_URL.format(job_id=match.group(1)) if match else None

    async def fetch_newest(self, keywords: Sequence[str]) -> list[Job]:
        """The newest jobs matching any of the keywords, newest first, without duplicates.

        For the channel alerts, which pick the software roles themselves: one
        request per keyword, each the newest MAX_PAGE_SIZE matches.

        A keyword whose search fails is skipped: the others' jobs are still returned.
        Raises only when no keyword's search worked, or when ZenRows refuses.
        """
        items: list = []
        failed: list[SourceError] = []
        succeeded = 0
        for keyword in keywords:
            try:
                items.extend(await self._search(1, MAX_PAGE_SIZE, keyword.replace("'", "''"), LATEST_SORT_BY))
            except ZenRowsLimitError:
                raise  # every later request would be refused too
            except SourceError as exc:
                log.warning("%s: search for %r failed: %s", NAME, keyword, exc)
                failed.append(exc)
                if len(failed) >= MAX_FAILED_KEYWORDS:
                    break
            else:
                succeeded += 1
        if failed and not succeeded:
            raise failed[-1]
        if failed:
            log.warning("%s: using the jobs of %d of %d keyword searches (%d failed)",
                        NAME, succeeded, len(keywords), len(failed))
        return parse_search_response(items)
