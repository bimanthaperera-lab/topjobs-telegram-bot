"""Site parsers and the polite HTTP client (no network)."""

import asyncio

import httpx
import pytest

from jobbot.sources import SourceError
from jobbot.sources.http import PoliteHttpClient
from jobbot.sources.topjobs import TopJobsSource, parse_search_page
from jobbot.sources.xpressjobs import XpressJobsSource, parse_search_response

# Trimmed copy of a real TopJobs.lk results row structure.
TOPJOBS_ROW = """
<tr id="tr{i}" onclick="createAlert('{i}','{ac}','{jc}','{ec}','SESSION')">
<td width="1%" nowrap align="center">{n}</td>
<td width="5%" align="center">{ref}</td>
<td width="28%">
<span id="hdnJC{i}" hidden>{jc}</span>
<span id="hdnEC{i}" hidden>{ec}</span>
<span id="hdnAC{i}" hidden>{ac}</span>
<h2><span>{title} </span></h2>
<h1>{company}</h1>
</td>
<td width="35%">{note}&nbsp;</td>
<td width="12%" nowrap> {opening}</td>
<!-- <td noowrap class="add_startdate"><b>Opening Date:</b> {opening}</td> -->
<td width="12%" nowrap> {closing}</td>
<td width="8%" nowrap> {town}</td>
</tr>
"""


def topjobs_page(rows):
    body = "".join(TOPJOBS_ROW.format(i=i, n=i + 1, **r) for i, r in enumerate(rows))
    return f"<html><form action='applicant/vacancybyfunctionalarea.jsp'></form><table>{body}</table></html>"


ROW_A = dict(ac="DEFZZZ", jc="0001566742", ec="DEFZZZ", ref="1566742",
             title="Assistant Manager - Management Accountant", company="GlobalSync Solutions (Pvt) Ltd",
             note="Please refer the vacancy", opening="Mon Nov 02 2026", closing="Mon Nov 16 2026", town="Kelaniya")
ROW_B = dict(ac="0000000375", jc="0001566731", ec="0000000492", ref="1566731",
             title="Accountant &amp; Analyst", company="Jobfactory",
             note="Overseas opportunity", opening="Sun Nov 01 2026", closing="Mon Nov 09 2026", town="")


def test_topjobs_parses_rows_in_order():
    jobs = parse_search_page(topjobs_page([ROW_A, ROW_B]))
    assert [j.title for j in jobs] == ["Assistant Manager - Management Accountant", "Accountant & Analyst"]
    a, b = jobs
    assert a.url == ("https://www.topjobs.lk/employer/JobAdvertismentServlet?rid=0&ac=DEFZZZ&jc=0001566742"
                     "&ec=DEFZZZ&pg=applicant/vacancybyfunctionalarea.jsp")
    assert a.company == "GlobalSync Solutions (Pvt) Ltd"
    assert a.location == "Kelaniya"
    assert a.posted == "Mon Nov 02 2026" and a.closing == "Mon Nov 16 2026"
    assert a.description is None  # boilerplate note dropped
    assert a.source == "TopJobs.lk"
    assert b.description == "Overseas opportunity"
    assert b.location is None


def test_topjobs_skips_incomplete_and_duplicate_rows():
    broken = dict(ROW_B, jc="")
    jobs = parse_search_page(topjobs_page([ROW_A, broken, ROW_A]))
    assert len(jobs) == 1


def test_topjobs_empty_page():
    assert parse_search_page(topjobs_page([])) == []


XPRESS_ITEMS = [
    {"jobId": 345276, "jobTitle": "Accountant", "organizationName": "Tradlanka",
     "overview": "A minimum of 5 years experience. Please refer to the job advert for further information.",
     "createdDate": None, "expiryDateOnWebsite": "2026-11-06T01:58:11.077", "jobType": "Full-Time",
     "locations": " Colombo, Western Province", "archived": False},
    {"jobId": 348657, "jobTitle": "Junior Accountant", "organizationName": "SN Travel",
     "overview": "Please refer to the job advert for further information.",
     "createdDate": "2026-11-01T09:00:00", "expiryDateOnWebsite": "2026-11-16T00:00:00",
     "jobType": "Full-Time", "locations": "Colombo", "archived": False},
    {"jobId": 300000, "jobTitle": "Old", "archived": True},
    {"jobId": "bad", "jobTitle": "Bad id"},
    {"jobId": 340000, "jobTitle": ""},
]


def test_xpressjobs_maps_and_sorts_newest_first():
    jobs = parse_search_response(XPRESS_ITEMS)
    assert [j.url for j in jobs] == ["https://xpress.jobs/jobs/view/348657", "https://xpress.jobs/jobs/view/345276"]
    newest, older = jobs
    assert newest.posted == "Sun Nov 01 2026"
    assert newest.closing == "Mon Nov 16 2026"
    assert newest.description == "Full-Time"  # boilerplate overview removed
    assert older.description == "Full-Time. A minimum of 5 years experience."
    assert older.location == "Colombo, Western Province"
    assert older.posted is None
    assert older.source == "XpressJobs"


def test_xpressjobs_rejects_unexpected_shape():
    with pytest.raises(SourceError):
        parse_search_response({"error": "nope"})


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_topjobs_fetch_sends_keyword_and_limits():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text=topjobs_page([ROW_A, ROW_B]))

    http = PoliteHttpClient(mock_client(handler), 0)
    jobs = await TopJobsSource(http).fetch("x" * 60, limit=1)
    assert len(jobs) == 1
    assert seen["url"].endswith("txtKeyWord=" + "x" * 40)  # trimmed to the site's 40-char limit


async def test_topjobs_unexpected_page_raises():
    http = PoliteHttpClient(mock_client(lambda r: httpx.Response(200, text="<html>Access denied</html>")), 0)
    with pytest.raises(SourceError):
        await TopJobsSource(http).fetch("accountant", 10)


async def test_xpressjobs_fetch_params():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=XPRESS_ITEMS)

    http = PoliteHttpClient(mock_client(handler), 0)
    jobs = await XpressJobsSource(http).fetch("o'neil", limit=30)
    assert len(jobs) == 2
    assert seen["params"] == {"page": "1", "pageSize": "30", "keyword": "o''neil",
                              "sortBy": "ExpiryDateOnWebsite DESC"}


async def test_http_errors_become_source_errors():
    http = PoliteHttpClient(mock_client(lambda r: httpx.Response(503)), 0, retries=0)
    with pytest.raises(SourceError, match="HTTP 503"):
        await http.get("https://example.com/")

    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    http = PoliteHttpClient(mock_client(boom), 0, retries=0)
    with pytest.raises(SourceError, match="ConnectTimeout"):
        await http.get("https://example.com/")


async def test_requests_to_same_host_are_spaced_out():
    now = [0.0]
    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(round(secs, 3))
        now[0] += secs

    http = PoliteHttpClient(mock_client(lambda r: httpx.Response(200)), 2.0,
                            clock=lambda: now[0], sleep=fake_sleep)
    await asyncio.gather(http.get("https://a.lk/1"), http.get("https://a.lk/2"), http.get("https://b.lk/1"))
    # Second request to a.lk waits 2s; b.lk is a different host and doesn't wait.
    assert sleeps == [2.0]


async def test_xpressjobs_fetch_latest_pages_newest_first():
    pages = []

    def handler(request):
        params = dict(request.url.params)
        pages.append(params)
        start = 1000 - (int(params["page"]) - 1) * 100
        count = 100 if params["page"] == "1" else 5  # the second page comes back short
        return httpx.Response(200, json=[{"jobId": start - i, "jobTitle": f"Job {start - i}"} for i in range(count)])

    http = PoliteHttpClient(mock_client(handler), 0)
    jobs = await XpressJobsSource(http).fetch_latest(300)
    assert [p["page"] for p in pages] == ["1", "2"]  # stopped at the short page
    assert pages[0] == {"page": "1", "pageSize": "100", "keyword": "", "sortBy": "JobId DESC"}
    assert len(jobs) == 105 and jobs[0].url.endswith("/1000") and jobs[-1].url.endswith("/896")
