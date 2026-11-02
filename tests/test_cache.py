"""Keyword cache expiry and sharing of in-flight Actor runs."""

import asyncio

import pytest

from jobbot.job_service import JobSearchService

from .conftest import make_jobs

TTL = 6 * 3600


class SlowFetch:
    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()

    async def __call__(self, keyword, limit):
        self.calls += 1
        await self.release.wait()
        return make_jobs(limit, prefix=f"{keyword}-run{self.calls}")


class FailingFetch:
    def __init__(self):
        self.calls = 0

    async def __call__(self, keyword, limit):
        self.calls += 1
        raise RuntimeError("actor failed")


def service_with(storage, fetch):
    return JobSearchService(storage, fetch, cache_ttl_secs=TTL, sorted_newest_first=False)


async def test_cache_reused_until_ttl_expires(storage, clock):
    fetch = SlowFetch()
    fetch.release.set()
    service = service_with(storage, fetch)

    first = await service.get_jobs("accountant")
    clock.advance(TTL - 1)
    assert await service.get_jobs("accountant") == first
    assert fetch.calls == 1

    clock.advance(2)  # now past the TTL
    refreshed = await service.get_jobs("accountant")
    assert fetch.calls == 2
    assert refreshed != first


async def test_cache_is_per_keyword(storage):
    fetch = SlowFetch()
    fetch.release.set()
    service = service_with(storage, fetch)
    await service.get_jobs("accountant")
    await service.get_jobs("driver")
    assert fetch.calls == 2


async def test_concurrent_searches_share_one_run(storage):
    fetch = SlowFetch()
    service = service_with(storage, fetch)

    waiters = [asyncio.create_task(service.get_jobs("accountant")) for _ in range(3)]
    await asyncio.sleep(0)
    fetch.release.set()
    results = await asyncio.gather(*waiters)

    assert fetch.calls == 1
    assert results[0] == results[1] == results[2]


async def test_cancelled_waiter_does_not_cancel_shared_run(storage):
    fetch = SlowFetch()
    service = service_with(storage, fetch)

    a = asyncio.create_task(service.get_jobs("accountant"))
    b = asyncio.create_task(service.get_jobs("accountant"))
    await asyncio.sleep(0)
    a.cancel()
    fetch.release.set()
    assert len(await b) == 10
    assert fetch.calls == 1


async def test_failed_run_is_not_cached(storage):
    fetch = FailingFetch()
    service = service_with(storage, fetch)
    with pytest.raises(RuntimeError):
        await service.get_jobs("accountant")
    with pytest.raises(RuntimeError):
        await service.get_jobs("accountant")
    assert fetch.calls == 2


async def test_empty_result_is_cached(storage):
    calls = 0

    async def empty_fetch(keyword, limit):
        nonlocal calls
        calls += 1
        return []

    service = service_with(storage, empty_fetch)
    assert await service.get_jobs("nothing") == []
    assert await service.get_jobs("nothing") == []
    assert calls == 1
