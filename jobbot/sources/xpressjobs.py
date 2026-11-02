"""XpressJobs: the JSON API behind https://xpress.jobs.

Search: GET /api/jobs/searchJobs?page=1&pageSize=N&keyword=<kw>&sortBy=ExpiryDateOnWebsite DESC
returns a JSON array of jobs. The public sort options are by expiry date only,
so results are re-sorted by jobId (assigned in posting order) to get newest first.
The channel alerts (fetch_newest) instead ask for sortBy=JobId DESC, which
the API accepts: the newest matches first.

XpressJobs' terms of use forbid scraping without permission, so this source is
disabled by default (XPRESSJOBS_ENABLED=false). Enable it only once XpressJobs
has given written permission.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..models import Job
from . import SourceError
from .http import PoliteHttpClient

NAME = "XpressJobs"
SEARCH_URL = "https://xpress.jobs/api/jobs/searchJobs"
JOB_URL = "https://xpress.jobs/jobs/view/{job_id}"
SORT_BY = "ExpiryDateOnWebsite DESC"
MAX_PAGE_SIZE = 100  # one modest request; the API filters by keyword itself
LATEST_SORT_BY = "JobId DESC"  # not offered in the site's UI, but the API accepts it: newest first

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


class XpressJobsSource:
    name = NAME
    sorted_newest_first = True

    def __init__(self, http: PoliteHttpClient) -> None:
        self._http = http

    async def _search(self, page: int, page_size: int, keyword: str, sort_by: str) -> list:
        response = await self._http.get(
            SEARCH_URL,
            params={"page": page, "pageSize": page_size, "keyword": keyword, "sortBy": sort_by},
            headers={"Accept": "application/json"},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"{NAME}: response was not JSON") from exc
        if not isinstance(data, list):
            raise SourceError(f"{NAME}: unexpected response shape")
        return data

    async def fetch(self, keyword: str, limit: int) -> list[Job]:
        # The site's own frontend doubles single quotes before sending.
        data = await self._search(1, min(limit, MAX_PAGE_SIZE), keyword.replace("'", "''"), SORT_BY)
        return parse_search_response(data)[:limit]

    async def fetch_newest(self, keywords: Sequence[str]) -> list[Job]:
        """The newest jobs matching any of the keywords, newest first, without duplicates.

        For the channel alerts, which pick the software roles themselves: one
        request per keyword, each the newest MAX_PAGE_SIZE matches.
        """
        items: list = []
        for keyword in keywords:
            items.extend(await self._search(1, MAX_PAGE_SIZE, keyword.replace("'", "''"), LATEST_SORT_BY))
        return parse_search_response(items)
