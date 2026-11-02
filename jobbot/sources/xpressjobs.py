"""XpressJobs: the JSON API behind https://xpress.jobs.

Search: GET /api/jobs/searchJobs?page=1&pageSize=N&keyword=<kw>&sortBy=ExpiryDateOnWebsite DESC
returns a JSON array of jobs. The public sort options are by expiry date only,
so results are re-sorted by jobId (assigned in posting order) to get newest first.

XpressJobs' terms of use forbid scraping without permission, so this source is
disabled by default (XPRESSJOBS_ENABLED=false). Enable it only once XpressJobs
has given written permission.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..models import Job
from . import SourceError
from .http import PoliteHttpClient

NAME = "XpressJobs"
SEARCH_URL = "https://xpress.jobs/api/jobs/searchJobs"
JOB_URL = "https://xpress.jobs/jobs/view/{job_id}"
SORT_BY = "ExpiryDateOnWebsite DESC"

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

    async def fetch(self, keyword: str, limit: int) -> list[Job]:
        response = await self._http.get(
            SEARCH_URL,
            params={
                "page": 1,
                "pageSize": limit,
                # The site's own frontend doubles single quotes before sending.
                "keyword": keyword.replace("'", "''"),
                "sortBy": SORT_BY,
            },
            headers={"Accept": "application/json"},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SourceError(f"{NAME}: response was not JSON") from exc
        return parse_search_response(data)[:limit]
