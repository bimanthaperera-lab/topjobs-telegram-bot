"""First page / Show More paging, served from SQLite only."""

from jobbot.job_service import SESSION_MAX_AGE_SECS, paginate

from .conftest import FakeSource, make_jobs, make_service


def urls(jobs):
    return [j.url for j in jobs]


def test_paginate_splits_into_five_and_rest():
    first, rest = paginate(make_jobs(10))
    assert len(first) == 5 and len(rest) == 5
    first, rest = paginate(make_jobs(3))
    assert len(first) == 3 and rest == []


async def test_search_returns_five_and_stores_rest(storage):
    jobs = make_jobs(12)
    service = make_service(storage, FakeSource(jobs=jobs))

    result = await service.search(1, "accountant")
    assert urls(result.first_page) == urls(jobs[:5])
    assert result.more_session_id is not None
    assert len(f"more:{result.more_session_id}".encode()) < 64

    service.record_sent(1, "accountant", result.first_page)
    more = service.show_more(1, result.more_session_id)
    assert urls(more) == urls(jobs[5:10])


async def test_no_button_when_five_or_fewer(storage):
    service = make_service(storage, FakeSource(jobs=make_jobs(5)))
    result = await service.search(1, "accountant")
    assert len(result.first_page) == 5
    assert result.more_session_id is None


async def test_show_more_never_contacts_sites(storage):
    source = FakeSource(jobs=make_jobs(10))
    service = make_service(storage, source)
    result = await service.search(1, "accountant")
    assert source.calls == 1
    service.show_more(1, result.more_session_id)
    assert source.calls == 1


async def test_double_click_only_sends_once(storage):
    service = make_service(storage, FakeSource(jobs=make_jobs(10)))
    result = await service.search(1, "accountant")
    assert service.show_more(1, result.more_session_id)
    assert service.show_more(1, result.more_session_id) is None


async def test_other_user_cannot_use_session(storage):
    service = make_service(storage, FakeSource(jobs=make_jobs(10)))
    result = await service.search(1, "accountant")
    assert service.show_more(2, result.more_session_id) is None
    assert service.show_more(1, result.more_session_id) is not None


async def test_expired_session(storage, clock):
    service = make_service(storage, FakeSource(jobs=make_jobs(10)))
    result = await service.search(1, "accountant")
    clock.advance(SESSION_MAX_AGE_SECS + 1)
    assert service.show_more(1, result.more_session_id) is None


async def test_unknown_session(storage):
    service = make_service(storage, FakeSource(jobs=make_jobs(10)))
    assert service.show_more(1, "doesnotexist") is None


async def test_new_search_retires_old_button(storage):
    # Not newest-first, so the second search can still show older unsent jobs.
    service = make_service(storage, FakeSource(jobs=make_jobs(30), newest_first=False))
    first = await service.search(1, "accountant")
    service.record_sent(1, "accountant", first.first_page)
    second = await service.search(1, "accountant")
    assert second.more_session_id is not None
    assert service.show_more(1, first.more_session_id) is None
    assert service.show_more(1, second.more_session_id) is not None


async def test_second_search_skips_sent_jobs(storage):
    service = make_service(storage, FakeSource(jobs=make_jobs(10)))
    first = await service.search(1, "accountant")
    service.record_sent(1, "accountant", first.first_page)
    service.record_more_sent(1, service.show_more(1, first.more_session_id))

    again = await service.search(1, "accountant")
    assert again.first_page == []
    assert again.more_session_id is None


async def test_sent_history_is_per_user(storage):
    jobs = make_jobs(10)
    source = FakeSource(jobs=jobs)
    service = make_service(storage, source)
    r1 = await service.search(1, "accountant")
    service.record_sent(1, "accountant", r1.first_page)
    r2 = await service.search(2, "accountant")
    assert urls(r2.first_page) == urls(jobs[:5])
    assert source.calls == 1  # shared cache


async def test_last_sent_is_stored_per_site(storage):
    a = FakeSource("Site A")
    b = FakeSource("Site B")
    service = make_service(storage, a, b)
    result = await service.search(1, "accountant")
    service.record_sent(1, "accountant", result.first_page)
    last = storage.get_last_sent_map(1, "accountant")
    assert last["Site A"].url == a.jobs[0].url
    assert last["Site B"].url == b.jobs[0].url


async def test_new_jobs_only_after_saved_url(storage):
    """Second search after new postings appear shows only the new ones."""
    old = make_jobs(10, "old")
    new = make_jobs(2, "new")
    source = FakeSource(jobs=lambda kw, call: old if call == 1 else new + old)
    service = make_service(storage, source, cache_ttl_secs=0)  # always refetch
    first = await service.search(1, "accountant")
    service.record_sent(1, "accountant", first.first_page)  # jobs old[0..4]; old[5..9] never sent

    second = await service.search(1, "accountant")
    # Stops at old[0] (saved most recent), so the unsent older old[5..9] are not re-offered.
    assert urls(second.first_page) == urls(new)


async def test_one_site_down_still_returns_other(storage):
    good = FakeSource("Site A")
    bad = FakeSource("Site B", fail=True)
    service = make_service(storage, good, bad)
    result = await service.search(1, "accountant")
    assert len(result.first_page) == 5
    assert all(j.source == "Site A" for j in result.first_page)
    assert result.failed_sources == ["Site B"]
