import pytest

from jobbot.rate_limit import (
    DAY,
    HOUR,
    REASON_COOLDOWN,
    REASON_GLOBAL_FETCHES,
    REASON_HOURLY,
    REASON_USER_FETCHES,
    RateLimitConfig,
    RateLimiter,
    RateLimitExceeded,
    format_wait,
    parse_admin_ids,
)

from .conftest import FakeSource, make_jobs, make_service


def limiter(storage, **kw):
    cfg = dict(cooldown_secs=0, searches_per_hour=0, user_fetches_per_day=0, global_fetches_per_day=0)
    cfg.update(kw)
    return RateLimiter(storage, RateLimitConfig(**cfg))


def test_cooldown(storage, clock):
    rl = limiter(storage, cooldown_secs=10)
    assert rl.check_search(1).allowed
    rl.record_search(1)
    d = rl.check_search(1)
    assert not d.allowed and d.reason == REASON_COOLDOWN and d.retry_after_secs == 10
    assert rl.check_search(2).allowed  # per user
    clock.advance(10)
    assert rl.check_search(1).allowed


def test_hourly_sliding_window(storage, clock):
    rl = limiter(storage, searches_per_hour=3)
    for _ in range(3):
        assert rl.check_search(1).allowed
        rl.record_search(1)
        clock.advance(60)
    d = rl.check_search(1)
    assert not d.allowed and d.reason == REASON_HOURLY
    assert d.retry_after_secs == HOUR - 180
    clock.advance(d.retry_after_secs)
    assert rl.check_search(1).allowed  # oldest event slid out


def test_user_fetch_budget(storage, clock):
    rl = limiter(storage, user_fetches_per_day=2)
    rl.record_fetch(1)
    rl.record_fetch(1)
    d = rl.check_fetch(1)
    assert not d.allowed and d.reason == REASON_USER_FETCHES
    assert rl.check_fetch(2).allowed
    clock.advance(DAY)
    assert rl.check_fetch(1).allowed


def test_global_fetch_budget_applies_to_everyone(storage):
    rl = limiter(storage, global_fetches_per_day=2, admin_user_ids=frozenset({99}))
    rl.record_fetch(1)
    rl.record_fetch(2)
    assert rl.check_fetch(3).reason == REASON_GLOBAL_FETCHES
    assert rl.check_fetch(99).reason == REASON_GLOBAL_FETCHES


def test_admin_skips_per_user_limits(storage):
    rl = limiter(storage, cooldown_secs=60, searches_per_hour=1, user_fetches_per_day=1,
                 admin_user_ids=frozenset({99}))
    for _ in range(5):
        assert rl.check_search(99).allowed
        rl.record_search(99)
        assert rl.check_fetch(99).allowed
        rl.record_fetch(99)


def test_zero_disables_limits(storage):
    rl = limiter(storage)
    for _ in range(50):
        rl.record_search(1)
        rl.record_fetch(1)
    assert rl.check_search(1).allowed
    assert rl.check_fetch(1).allowed


def test_purge_removes_old_events(storage, clock):
    rl = limiter(storage)
    rl.record_search(1)
    clock.advance(DAY + 1)
    rl.record_search(1)
    assert rl.purge() == 1


def keyword_jobs(name):
    return lambda keyword, call: make_jobs(10, prefix=f"{name}-{keyword}", source=name)


async def test_cache_hits_do_not_use_fetch_budget(storage):
    source = FakeSource(jobs=keyword_jobs("Site A"))
    rl = limiter(storage, user_fetches_per_day=1)
    service = make_service(storage, source, limiter=rl)

    await service.search(1, "accountant")  # uses the one fetch
    await service.search(1, "accountant")  # cache hit, allowed
    await service.search(1, "accountant")
    assert source.calls == 1

    with pytest.raises(RateLimitExceeded) as exc:
        await service.search(1, "driver")  # would need a new fetch
    assert exc.value.decision.reason == REASON_USER_FETCHES
    assert source.calls == 1


async def test_one_search_counts_once_for_all_sites(storage):
    a, b = FakeSource("Site A"), FakeSource("Site B")
    rl = limiter(storage, user_fetches_per_day=1)
    service = make_service(storage, a, b, limiter=rl)
    await service.search(1, "accountant")  # contacts both sites, one budget unit
    assert a.calls == 1 and b.calls == 1


async def test_blocked_user_can_still_use_keyword_cached_by_others(storage):
    source = FakeSource(jobs=keyword_jobs("Site A"))
    rl = limiter(storage, user_fetches_per_day=1)
    service = make_service(storage, source, limiter=rl)
    await service.search(1, "accountant")
    await service.search(2, "driver")
    result = await service.search(1, "driver")  # user 1 is out of fetches but driver is cached
    assert result.first_page
    assert source.calls == 2


async def test_blocked_user_gets_partially_cached_results(storage, clock):
    a = FakeSource("Site A")
    b = FakeSource("Site B", fail=True)
    rl = limiter(storage, user_fetches_per_day=1)
    service = make_service(storage, a, b, limiter=rl)
    await service.search(1, "accountant")  # A cached, B failed (not cached)
    result = await service.search(1, "accountant")  # B needs a fetch but budget is spent
    assert result.first_page  # served from A's cache instead of refusing
    assert b.calls == 1


def test_parse_admin_ids():
    assert parse_admin_ids("") == frozenset()
    assert parse_admin_ids("1, 2;3") == frozenset({1, 2, 3})
    with pytest.raises(ValueError):
        parse_admin_ids("abc")


@pytest.mark.parametrize(("secs", "text"), [(1, "1 second"), (45, "45 seconds"), (61, "2 minutes"),
                                            (3600, "1 hour"), (3601, "2 hours")])
def test_format_wait(secs, text):
    assert format_wait(secs) == text
