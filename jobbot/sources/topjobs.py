"""TopJobs.lk: server-rendered HTML search results.

Search: GET /applicant/vacancybyfunctionalarea.jsp?txtKeyWord=<keyword>
returns every matching vacancy in one page, newest first (by opening date).

Each result row looks like:
    <tr id="trN" onclick="createAlert(...)">
      <td>N</td> <td>ref no</td>
      <td><span id="hdnJCN">jc</span><span id="hdnECN">ec</span><span id="hdnACN">ac</span>
          <h2><span>Title</span></h2><h1>Employer</h1></td>
      <td>short note</td> <td>opening date</td> <td>closing date</td> <td>town</td>
    </tr>
The real job description is an image on the detail page, so only the short note is text.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from ..models import Job
from . import SourceError
from .http import PoliteHttpClient

NAME = "TopJobs.lk"
BASE_URL = "https://www.topjobs.lk"
SEARCH_URL = f"{BASE_URL}/applicant/vacancybyfunctionalarea.jsp"
DETAIL_URL = f"{BASE_URL}/employer/JobAdvertismentServlet"
MAX_KEYWORD_LENGTH = 40  # the site's own input limit

_ROW_ID = re.compile(r"^tr\d+$")
_BOILERPLATE_NOTE = re.compile(r"^\s*please refer( to)? the (vacancy|advert(isement)?)\.?\s*$", re.I)
_WHITESPACE = re.compile(r"\s+")


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = _WHITESPACE.sub(" ", text.replace("\xa0", " ")).strip()
    return text or None


def _hidden(row, prefix: str) -> str | None:
    span = row.find("span", id=re.compile(rf"^{prefix}\d+$"))
    return _clean(span.get_text()) if span else None


def detail_url(ac: str, jc: str, ec: str) -> str:
    # safe="/" keeps the link identical to the site's own ("pg=applicant/...").
    query = urlencode(
        {"rid": 0, "ac": ac, "jc": jc, "ec": ec, "pg": "applicant/vacancybyfunctionalarea.jsp"}, safe="/"
    )
    return f"{DETAIL_URL}?{query}"


def parse_search_page(html: str) -> list[Job]:
    """Parse a search results page into Jobs, in page order (newest first)."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[Job] = []
    seen: set[str] = set()
    for row in soup.find_all("tr", id=_ROW_ID):
        jc, ec, ac = _hidden(row, "hdnJC"), _hidden(row, "hdnEC"), _hidden(row, "hdnAC")
        title_tag = row.find("h2")
        title = _clean(title_tag.get_text()) if title_tag else None
        if not (jc and ec and ac and title):
            continue
        url = detail_url(ac, jc, ec)
        if url in seen:
            continue
        seen.add(url)
        company_tag = row.find("h1")
        cells = [_clean(td.get_text()) for td in row.find_all("td", recursive=False)]
        note = cells[3] if len(cells) > 3 else None
        jobs.append(
            Job(
                url=url,
                title=title,
                company=_clean(company_tag.get_text()) if company_tag else None,
                location=cells[6] if len(cells) > 6 else None,
                posted=cells[4] if len(cells) > 4 else None,
                closing=cells[5] if len(cells) > 5 else None,
                description=None if not note or _BOILERPLATE_NOTE.match(note) else note,
                source=NAME,
            )
        )
    return jobs


def looks_like_results_page(html: str) -> bool:
    return "vacancybyfunctionalarea" in html or "JobAdvertismentServlet" in html


class TopJobsSource:
    name = NAME
    sorted_newest_first = True

    def __init__(self, http: PoliteHttpClient) -> None:
        self._http = http

    async def fetch(self, keyword: str, limit: int) -> list[Job]:
        response = await self._http.get(SEARCH_URL, params={"txtKeyWord": keyword[:MAX_KEYWORD_LENGTH]})
        html = response.text
        if not looks_like_results_page(html):
            raise SourceError(f"{NAME}: unexpected page (layout changed or blocked)")
        # The page is ~400 KB; parse off the event loop.
        jobs = await asyncio.to_thread(parse_search_page, html)
        return jobs[:limit]
