"""A slow site (XpressJobs) is fetched in the background; searches never wait for it."""

import httpx

from jobbot.job_service import REFRESH_CHECK_SECS
from jobbot.sources.http import PoliteHttpClient
from jobbot.sources.xpressjobs import XpressJobsSource

from .conftest import FakeSource, make_jobs, make_service

TTL = 3600


def background_source(**kwargs):
    source = FakeSource("Slow", jobs=make_jobs(10, prefix="slow", source="Slow"), **kwargs)
    source.keyword_independent = source.refreshed_in_background = True
    return source


async def test_search_never_fetches_a_background_site(storage):
    fast, slow = FakeSource("Fast"), background_source()
    service = make_service(storage, fast, slow, cache_ttl_secs=TTL)

    result = await service.search(1, "developer")
    assert slow.calls == 0
    assert {j.source for j in result.first_page} == {"Fast"}
    assert result.failed_sources == [] and result.stale_sources == []  # not ready yet is not a failure

    await service.refresh_due()
    assert slow.calls == 1
    result = await service.search(1, "qa")
    assert "Slow" in {j.source for j in result.first_page}
    assert slow.calls == 1  # every keyword uses the one refreshed list


async def test_refresh_only_when_due(storage, clock):
    slow = background_source()
    service = make_service(storage, FakeSource("Fast"), slow, cache_ttl_secs=TTL)
    await service.refresh_due()
    await service.refresh_due()
    assert slow.calls == 1
    clock.advance(TTL / 2 + 1)
    await service.refresh_due()
    assert slow.calls == 2


async def test_failed_refresh_keeps_older_list_and_is_retried(storage, clock):
    slow = background_source()
    service = make_service(storage, FakeSource("Fast"), slow, cache_ttl_secs=TTL)
    await service.refresh_due()
    slow.fail = True
    clock.advance(TTL + 1)
    await service.refresh_due()  # logged, not raised
    result = await service.search(1, "developer")
    assert "Slow" in result.stale_sources  # shown, marked as older
    clock.advance(REFRESH_CHECK_SECS)
    slow.fail = False
    await service.refresh_due()
    assert slow.calls == 3


async def test_xpressjobs_pool_ignores_the_keyword():
    keywords = []

    def handler(request):
        keywords.append(request.url.params["keyword"])
        return httpx.Response(200, json=[{"jobId": len(keywords), "jobTitle": f"Dev {len(keywords)}"}])

    source = XpressJobsSource(PoliteHttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), 0),
                              pool_keywords=("software", "qa"))
    assert source.keyword_independent and source.refreshed_in_background
    jobs = await source.fetch("anything", 1)
    assert keywords == ["software", "qa"]
    assert [j.title for j in jobs] == ["Dev 2", "Dev 1"]  # the whole pool, newest first

    plain = XpressJobsSource(PoliteHttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), 0))
    assert not plain.keyword_independent and not plain.refreshed_in_background
