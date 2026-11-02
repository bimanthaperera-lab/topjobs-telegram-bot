"""Per-site cache expiry and sharing of in-flight requests."""

import asyncio

import pytest

from jobbot.job_service import FetchError
from jobbot.models import Job

from .conftest import FakeSource, make_jobs, make_service

TTL = 6 * 3600


def changing_jobs(name):
    return lambda keyword, call: make_jobs(10, prefix=f"{name}-{keyword}-run{call}", source=name)


async def test_cache_reused_until_ttl_expires(storage, clock):
    source = FakeSource(jobs=changing_jobs("Site A"))
    service = make_service(storage, source, cache_ttl_secs=TTL)

    first = await service.get_jobs("accountant")
    clock.advance(TTL - 1)
    assert await service.get_jobs("accountant") == first
    assert source.calls == 1

    clock.advance(2)  # now past the TTL
    refreshed = await service.get_jobs("accountant")
    assert source.calls == 2
    assert refreshed != first


async def test_cache_is_per_keyword(storage):
    source = FakeSource(jobs=changing_jobs("Site A"))
    service = make_service(storage, source)
    await service.get_jobs("accountant")
    await service.get_jobs("driver")
    assert source.calls == 2


async def test_concurrent_searches_share_one_request_per_site(storage):
    gate = asyncio.Event()
    a = FakeSource("Site A", gate=gate)
    b = FakeSource("Site B", gate=gate)
    service = make_service(storage, a, b)

    waiters = [asyncio.create_task(service.get_jobs("accountant")) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(*waiters)

    assert a.calls == 1 and b.calls == 1
    assert results[0] == results[1] == results[2]
    assert len(results[0]) == 60


async def test_cancelled_waiter_does_not_cancel_shared_request(storage):
    gate = asyncio.Event()
    source = FakeSource(gate=gate)
    service = make_service(storage, source)

    first = asyncio.create_task(service.get_jobs("accountant"))
    second = asyncio.create_task(service.get_jobs("accountant"))
    await asyncio.sleep(0)
    first.cancel()
    gate.set()
    assert len(await second) == 30
    assert source.calls == 1


async def test_failed_site_is_not_cached(storage):
    source = FakeSource(fail=True)
    service = make_service(storage, source)
    with pytest.raises(FetchError):
        await service.get_jobs("accountant")
    with pytest.raises(FetchError):
        await service.get_jobs("accountant")
    assert source.calls == 2


async def test_working_site_cached_even_if_other_fails(storage):
    good = FakeSource("Site A")
    bad = FakeSource("Site B", fail=True)
    service = make_service(storage, good, bad)
    await service.get_jobs("accountant")
    await service.get_jobs("accountant")
    assert good.calls == 1  # cached
    assert bad.calls == 2   # retried, not cached


async def test_all_sites_failing_raises(storage):
    service = make_service(storage, FakeSource("Site A", fail=True), FakeSource("Site B", fail=True))
    with pytest.raises(FetchError):
        await service.search(1, "accountant")


async def test_empty_result_is_cached(storage):
    source = FakeSource(jobs=[])
    service = make_service(storage, source)
    assert await service.get_jobs("nothing") == []
    assert await service.get_jobs("nothing") == []
    assert source.calls == 1


async def test_category_phrases_share_one_fetch_and_keep_priority(storage):
    titles = ["Data Entry Operator", "Business Data Analyst", "Senior Data Engineer", "Data Engineer"]
    source = FakeSource(jobs=[Job(url=f"https://x.lk/{i}", title=t, source="Site A") for i, t in enumerate(titles)])
    source.keyword_independent = True  # a whole-category list, like TopJobs with TOPJOBS_CATEGORY
    service = make_service(storage, source)
    result = await service.search(1, "📊 Data", any_of=("data engineer", "data analyst"))
    assert source.calls == 1
    assert [j.title for j in result.first_page] == ["Senior Data Engineer", "Data Engineer", "Business Data Analyst"]
