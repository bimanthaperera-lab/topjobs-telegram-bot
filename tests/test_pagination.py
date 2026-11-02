"""First page / Show More paging, served from SQLite only."""

from jobbot.job_service import SESSION_MAX_AGE_SECS, JobSearchService, paginate

from .conftest import make_jobs


class CountingFetch:
    def __init__(self, jobs):
        self.jobs = jobs
        self.calls = 0

    async def __call__(self, keyword, limit):
        self.calls += 1
        return self.jobs[:limit]


def make_service(storage, fetch):
    return JobSearchService(storage, fetch, cache_ttl_secs=3600, sorted_newest_first=False)


def test_paginate_splits_into_five_and_rest():
    first, rest = paginate(make_jobs(10))
    assert len(first) == 5 and len(rest) == 5
    first, rest = paginate(make_jobs(3))
    assert len(first) == 3 and rest == []


async def test_search_returns_five_and_stores_rest(storage):
    jobs = make_jobs(12)
    service = make_service(storage, CountingFetch(jobs))

    result = await service.search(1, "accountant")
    assert [j.url for j in result.first_page] == [j.url for j in jobs[:5]]
    assert result.more_session_id is not None
    assert len(f"more:{result.more_session_id}".encode()) < 64

    service.record_sent(1, "accountant", result.first_page)
    more = service.show_more(1, result.more_session_id)
    assert [j.url for j in more] == [j.url for j in jobs[5:10]]


async def test_no_button_when_five_or_fewer(storage):
    service = make_service(storage, CountingFetch(make_jobs(5)))
    result = await service.search(1, "accountant")
    assert len(result.first_page) == 5
    assert result.more_session_id is None


async def test_show_more_never_runs_actor(storage):
    fetch = CountingFetch(make_jobs(10))
    service = make_service(storage, fetch)
    result = await service.search(1, "accountant")
    assert fetch.calls == 1
    service.show_more(1, result.more_session_id)
    assert fetch.calls == 1


async def test_double_click_only_sends_once(storage):
    service = make_service(storage, CountingFetch(make_jobs(10)))
    result = await service.search(1, "accountant")
    assert service.show_more(1, result.more_session_id)
    assert service.show_more(1, result.more_session_id) is None


async def test_other_user_cannot_use_session(storage):
    service = make_service(storage, CountingFetch(make_jobs(10)))
    result = await service.search(1, "accountant")
    assert service.show_more(2, result.more_session_id) is None
    assert service.show_more(1, result.more_session_id) is not None


async def test_expired_session(storage, clock):
    service = make_service(storage, CountingFetch(make_jobs(10)))
    result = await service.search(1, "accountant")
    clock.advance(SESSION_MAX_AGE_SECS + 1)
    assert service.show_more(1, result.more_session_id) is None


async def test_unknown_session(storage):
    service = make_service(storage, CountingFetch(make_jobs(10)))
    assert service.show_more(1, "doesnotexist") is None


async def test_new_search_retires_old_button(storage):
    service = JobSearchService(storage, CountingFetch(make_jobs(20)), cache_ttl_secs=3600,
                               sorted_newest_first=False, jobs_per_search=20)
    first = await service.search(1, "accountant")
    service.record_sent(1, "accountant", first.first_page)
    second = await service.search(1, "accountant")
    assert second.more_session_id is not None
    assert service.show_more(1, first.more_session_id) is None
    assert service.show_more(1, second.more_session_id) is not None


async def test_new_search_retires_old_button_even_without_new_button(storage):
    service = make_service(storage, CountingFetch(make_jobs(10)))
    first = await service.search(1, "accountant")
    service.record_sent(1, "accountant", first.first_page)
    second = await service.search(1, "accountant")  # shows jobs 5-9 directly
    assert second.more_session_id is None
    assert service.show_more(1, first.more_session_id) is None


async def test_second_search_skips_sent_jobs(storage):
    jobs = make_jobs(10)
    service = make_service(storage, CountingFetch(jobs))
    first = await service.search(1, "accountant")
    service.record_sent(1, "accountant", first.first_page)
    service.record_more_sent(1, service.show_more(1, first.more_session_id))

    again = await service.search(1, "accountant")
    assert again.first_page == []
    assert again.more_session_id is None


async def test_sent_history_is_per_user(storage):
    jobs = make_jobs(10)
    fetch = CountingFetch(jobs)
    service = make_service(storage, fetch)
    r1 = await service.search(1, "accountant")
    service.record_sent(1, "accountant", r1.first_page)
    r2 = await service.search(2, "accountant")
    assert [j.url for j in r2.first_page] == [j.url for j in jobs[:5]]
    assert fetch.calls == 1  # shared cache


async def test_last_sent_is_stored(storage):
    jobs = make_jobs(10)
    service = make_service(storage, CountingFetch(jobs))
    result = await service.search(1, "accountant")
    service.record_sent(1, "accountant", result.first_page)
    last = storage.get_last_sent(1, "accountant")
    assert last.url == jobs[0].url and last.title == jobs[0].title
