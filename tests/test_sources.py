"""Site parsers and the polite HTTP client (no network)."""

import asyncio

import httpx
import pytest

from jobbot.sources import SourceError
from jobbot.sources.http import PoliteHttpClient
from jobbot.sources.topjobs import TopJobsSource, parse_search_page
from jobbot.sources.xpressjobs import (
    ZENROWS_TIMEOUT_SECS,
    XpressJobsSource,
    ZenRowsLimitError,
    parse_search_response,
)

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
     "locations": " Colombo, Western Province", "archived": False,
     "hasLogo": True, "logoImageUrl": "https://cdn.xpress.jobs/Organizations/logo_1.jpg"},
    {"jobId": 348657, "jobTitle": "Junior Accountant", "organizationName": "SN Travel",
     "overview": "Please refer to the job advert for further information.",
     "createdDate": "2026-11-01T09:00:00", "expiryDateOnWebsite": "2026-11-16T00:00:00",
     "jobType": "Full-Time", "locations": "Colombo", "archived": False,
     "hasLogo": False, "logoImageUrl": None},
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
    assert older.logo_url == "https://cdn.xpress.jobs/Organizations/logo_1.jpg"
    assert newest.logo_url is None


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


async def test_xpressjobs_through_zenrows():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=XPRESS_ITEMS)

    http = PoliteHttpClient(mock_client(handler), 0)
    jobs = await XpressJobsSource(http, zenrows_api_key="KEY").fetch("o'neil", limit=30)
    assert len(jobs) == 2
    assert seen["url"].host == "api.zenrows.com"
    assert seen["url"].params["apikey"] == "KEY"
    target = httpx.URL(seen["url"].params["url"])
    assert str(target.copy_with(query=None)) == "https://xpress.jobs/api/jobs/searchJobs"
    assert dict(target.params) == {"page": "1", "pageSize": "30", "keyword": "o''neil",
                                   "sortBy": "ExpiryDateOnWebsite DESC"}
    assert seen["timeout"]["read"] == ZENROWS_TIMEOUT_SECS >= 60  # the client's 20s timed out


def keyword_handler(failing: set[str], calls: list[str]):
    """XpressJobs answering with one job per keyword, and 503 for the failing ones."""
    def handler(request):
        keyword = request.url.params["keyword"]
        calls.append(keyword)
        if keyword in failing:
            return httpx.Response(503)
        return httpx.Response(200, json=[{"jobId": len(calls), "jobTitle": f"{keyword} job"}])
    return handler


async def test_fetch_newest_skips_a_failed_keyword():
    calls = []
    http = PoliteHttpClient(mock_client(keyword_handler({"data"}, calls)), 0, retries=0)
    jobs = await XpressJobsSource(http).fetch_newest(["software", "data", "network"])
    assert calls == ["software", "data", "network"]
    assert [j.title for j in jobs] == ["network job", "software job"]


async def test_fetch_newest_gives_up_after_repeated_failures():
    calls = []
    http = PoliteHttpClient(mock_client(keyword_handler({"data", "cloud"}, calls)), 0, retries=0)
    jobs = await XpressJobsSource(http).fetch_newest(["software", "data", "cloud", "network"])
    assert calls == ["software", "data", "cloud"]  # stopped after MAX_FAILED_KEYWORDS failures
    assert [j.title for j in jobs] == ["software job"]


async def test_fetch_newest_raises_when_every_keyword_fails():
    calls = []
    http = PoliteHttpClient(mock_client(keyword_handler({"software", "data"}, calls)), 0, retries=0)
    with pytest.raises(SourceError, match="HTTP 503"):
        await XpressJobsSource(http).fetch_newest(["software", "data", "network"])


async def test_fetch_newest_stops_when_zenrows_refuses():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(402)

    source = XpressJobsSource(PoliteHttpClient(mock_client(handler), 0), zenrows_api_key="KEY")
    with pytest.raises(ZenRowsLimitError):
        await source.fetch_newest(["software", "data"])
    assert calls == ["api.zenrows.com"]


@pytest.mark.parametrize("status", [402, 429])
async def test_zenrows_limit_is_its_own_error(status):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(status)

    source = XpressJobsSource(PoliteHttpClient(mock_client(handler), 0), zenrows_api_key="KEY")
    with pytest.raises(ZenRowsLimitError):
        await source.fetch("developer", 10)
    with pytest.raises(ZenRowsLimitError):  # paused: no second request
        await source.fetch("developer", 10)
    assert calls == ["api.zenrows.com"]


async def test_xpressjobs_ad_image_url_needs_no_request():
    source = XpressJobsSource(PoliteHttpClient(mock_client(lambda r: httpx.Response(500)), 0))
    assert await source.ad_image_url("https://xpress.jobs/jobs/view/349032") == \
        "https://xpress.jobs/api/vacancy/getJobDescriptionImage?jobId=349032"
    assert await source.ad_image_url("https://example.com/other") is None


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


async def test_xpressjobs_fetch_newest_one_search_per_keyword():
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        base = 100 if params["keyword"] == "software" else 200
        return httpx.Response(200, json=[{"jobId": base + i, "jobTitle": f"Job {base + i}"} for i in range(2)]
                              + [{"jobId": 150, "jobTitle": "In both"}])

    http = PoliteHttpClient(mock_client(handler), 0)
    jobs = await XpressJobsSource(http).fetch_newest(["software", "o'neil"])
    assert seen[0] == {"page": "1", "pageSize": "100", "keyword": "software", "sortBy": "JobId DESC"}
    assert seen[1]["keyword"] == "o''neil"
    assert [j.url.rsplit("/", 1)[1] for j in jobs] == ["201", "200", "150", "101", "100"]  # merged, newest first
