"""Input/output mapping for the `fanndev/topjobs-lk-scraper` Actor.

Inspected from the Actor's published build (0.0.2) and README:

Input (search mode)
    mode              "search"
    keyword           free-text search term
    maxResults        result limit (1-5000, default 50)
    fetchFullDetails  one extra request per job for location/jobType/jsonLd (no extra billed event)

Output dataset item -> Job
    position                              -> title
    employer                              -> company
    location (full) / town (compact)      -> location
    openingDate (compact) / jsonLd.datePosted -> posted
    jsonLd.description, else jobType + closingDate -> description
    detailUrl                             -> url
    _error present                        -> item skipped

The Actor documents no sort order and has no "newest first" option, so
ACTOR_SORTS_NEWEST is False and early stopping at the saved URL is disabled.
If you switch to an Actor that can sort by date, set it to True and add the
sort option to build_run_input().
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .models import Job

ACTOR_SORTS_NEWEST = False

_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")
# TopJobs.lk publishes real descriptions as images; jsonLd usually carries only this stub.
_BOILERPLATE = re.compile(r"please refer the full details of the job posting as given by the employer\.?", re.I)


def build_run_input(keyword: str, limit: int, fetch_full_details: bool) -> dict[str, Any]:
    return {
        "mode": "search",
        "keyword": keyword,
        "maxResults": limit,
        "fetchFullDetails": fetch_full_details,
    }


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    value = _WHITESPACE.sub(" ", html.unescape(_TAG.sub(" ", value))).strip()
    return value or None


def _json_ld(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("jsonLd")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if isinstance(raw, list):
        raw = next((x for x in raw if isinstance(x, dict)), None)
    return raw if isinstance(raw, dict) else {}


def _description(item: dict[str, Any], json_ld: dict[str, Any]) -> str | None:
    text = _text(json_ld.get("description"))
    if text:
        text = _BOILERPLATE.sub("", text).strip(" .…") or None
    if text:
        return text
    parts: list[str] = []
    job_type = item.get("jobType")
    if isinstance(job_type, list):
        badges = [b for b in (_text(x) for x in job_type) if b]
        if badges:
            parts.append(" · ".join(badges))
    closing = _text(item.get("closingDate"))
    if closing:
        parts.append(f"Closes {closing}")
    return ". ".join(parts) or None


def _is_http_url(value: str | None) -> bool:
    return bool(value) and value.lower().startswith(("https://", "http://"))


def map_item(item: Any) -> Job | None:
    """Convert one dataset item into a Job, or None if it is unusable."""
    if not isinstance(item, dict) or item.get("_error"):
        return None
    raw_url = item.get("detailUrl")
    # Not run through _text(): unescaping could corrupt query strings such as "&copy=...".
    url = raw_url.strip() if isinstance(raw_url, str) else None
    title = _text(item.get("position"))
    if not _is_http_url(url) or not title:
        return None
    json_ld = _json_ld(item)
    return Job(
        url=url,
        title=title,
        company=_text(item.get("employer")),
        location=_text(item.get("location")) or _text(item.get("town")),
        posted=_text(item.get("openingDate")) or _text(json_ld.get("datePosted")),
        description=_description(item, json_ld),
    )
