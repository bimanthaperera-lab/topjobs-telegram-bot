"""Retries, pausing a site that blocks us, stale-cache fallback and admin alerts."""

import asyncio
from datetime import datetime, timezone

import httpx
import pytest
from telegram.error import Forbidden

from jobbot.job_service import FetchError
from jobbot.site_health import AdminNotifier, SiteHealth
from jobbot.sources import SiteBlockedError, SitePausedError, SourceError
from jobbot.sources.http import PoliteHttpClient, retry_after_secs

from .conftest import FakeSource, make_service


class Harness:
    """PoliteHttpClient over a scripted transport with a fake clock."""

    def __init__(self, responses, **kwargs):
        self.responses = list(responses)
        self.requests = 0
        self.now = 0.0
        self.sleeps = []

        def handler(request):
            self.requests += 1
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        async def sleep(secs):
            self.sleeps.append(secs)
            self.now += secs

        kwargs.setdefault("retry_backoff_secs", 2.0)
        kwargs.setdefault("block_pause_secs", 1800)
        self.http = PoliteHttpClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), 0,
                                     clock=lambda: self.now, sleep=sleep, **kwargs)

    async def get(self):
        return await self.http.get("https://site.lk/search")


def timeout():
    return httpx.ConnectTimeout("slow")


# --- retries -----------------------------------------------------------------

async def test_retries_5xx_with_backoff_then_succeeds():
    h = Harness([httpx.Response(503), httpx.Response(502), httpx.Response(200, text="ok")])
    response = await h.get()
    assert response.text == "ok"
    assert h.requests == 3
    assert h.sleeps == [2.0, 4.0]


async def test_retries_timeouts():
    h = Harness([timeout(), httpx.Response(200)])
    await h.get()
    assert h.requests == 2


async def test_gives_up_after_retries():
    h = Harness([httpx.Response(500)] * 3)
    with pytest.raises(SourceError, match="after 3 attempts"):
        await h.get()
    assert h.requests == 3


async def test_does_not_retry_client_errors():
    h = Harness([httpx.Response(404)])
    with pytest.raises(SourceError, match="HTTP 404"):
        await h.get()
    assert h.requests == 1


# --- pausing a site that blocks or rate-limits us ------------------------------

async def test_429_pauses_site_and_honours_retry_after():
    h = Harness([httpx.Response(429, headers={"Retry-After": "7200"}), httpx.Response(200)])
    with pytest.raises(SiteBlockedError) as exc:
        await h.get()
    assert exc.value.status == 429 and exc.value.pause_secs == 7200
    assert h.requests == 1  # never retried

    with pytest.raises(SitePausedError):
        await h.get()
    assert h.requests == 1  # paused: no request sent at all

    h.now += 7200
    await h.get()
    assert h.requests == 2


async def test_403_uses_default_pause():
    h = Harness([httpx.Response(403)])
    with pytest.raises(SiteBlockedError) as exc:
        await h.get()
    assert exc.value.pause_secs == 1800
    assert h.http.paused_for("site.lk") == 1800


async def test_short_retry_after_does_not_shorten_default_pause():
    h = Harness([httpx.Response(429, headers={"Retry-After": "5"})])
    with pytest.raises(SiteBlockedError) as exc:
        await h.get()
    assert exc.value.pause_secs == 1800


async def test_pause_is_per_host():
    h = Harness([httpx.Response(403), httpx.Response(200)])
    with pytest.raises(SiteBlockedError):
        await h.get()
    await h.http.get("https://other.lk/")  # different site still works
    assert h.requests == 2


def test_retry_after_parsing():
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    assert retry_after_secs("120") == 120
    assert retry_after_secs("Mon, 02 Nov 2026 13:00:00 GMT", now) == 3600
    assert retry_after_secs("garbage") is None
    assert retry_after_secs(None) is None


# --- stale-cache fallback --------------------------------------------------------

class Flaky(FakeSource):
    """Succeeds until .fail is set."""


async def test_stale_results_used_when_site_fails(storage, clock):
    source = Flaky()
    service = make_service(storage, source, cache_ttl_secs=3600, stale_max_secs=48 * 3600)
    await service.get_jobs("accountant")  # fills the cache
    clock.advance(3 * 3600)  # cache expired but within the stale window
    source.fail = True

    result = await service.search(1, "accountant")
    assert len(result.first_page) == 5
    assert result.stale_sources == ["Site A"]
    assert result.failed_sources == []


async def test_too_old_cache_is_not_used(storage, clock):
    source = Flaky()
    service = make_service(storage, source, cache_ttl_secs=3600, stale_max_secs=48 * 3600)
    await service.get_jobs("accountant")
    clock.advance(49 * 3600)
    source.fail = True
    with pytest.raises(FetchError):
        await service.search(1, "accountant")


async def test_empty_answer_from_one_site_is_not_a_total_failure(storage):
    service = make_service(storage, FakeSource("Site A", jobs=[]), FakeSource("Site B", fail=True))
    result = await service.search(1, "nothing")
    assert result.first_page == []
    assert result.failed_sources == ["Site B"]


async def test_stale_used_when_fetch_budget_is_spent(storage, clock):
    from jobbot.rate_limit import RateLimitConfig, RateLimiter

    a, b = FakeSource("Site A"), FakeSource("Site B")
    rl = RateLimiter(storage, RateLimitConfig(cooldown_secs=0, searches_per_hour=0,
                                              user_fetches_per_day=1, global_fetches_per_day=0))
    # A's cache lives 1h; B's entry is older but still within the stale window.
    service = make_service(storage, a, b, cache_ttl_secs=3600, limiter=rl)
    await service.search(2, "accountant")  # another user fills both caches
    clock.advance(2 * 3600)  # both expired
    await service.search(1, "driver")  # user 1 spends their only fetch
    result = await service.search(1, "accountant")  # needs fetches, budget spent -> stale
    assert result.first_page
    assert sorted(result.stale_sources) == ["Site A", "Site B"]


# --- site health and admin alerts ---------------------------------------------

def test_alert_after_n_failures_then_recovery():
    health = SiteHealth(alert_after_failures=3)
    err = SourceError("boom")
    assert health.record_failure("TopJobs.lk", err) is None
    assert health.record_failure("TopJobs.lk", err) is None
    alert = health.record_failure("TopJobs.lk", err)
    assert "TopJobs.lk" in alert and "3 times" in alert
    assert health.record_failure("TopJobs.lk", err) is None  # no repeat while down
    assert "working again" in health.record_success("TopJobs.lk")
    assert health.record_success("TopJobs.lk") is None


def test_block_alerts_immediately_and_pause_is_ignored():
    health = SiteHealth(alert_after_failures=3)
    alert = health.record_failure("XpressJobs", SiteBlockedError("xpress.jobs", 429, 1800))
    assert "429" in alert and "30 min" in alert
    assert health.record_failure("XpressJobs", SitePausedError("xpress.jobs", 900)) is None


def test_success_resets_failure_count():
    health = SiteHealth(alert_after_failures=2)
    health.record_failure("A", SourceError("x"))
    health.record_success("A")
    assert health.record_failure("A", SourceError("x")) is None


async def test_service_sends_one_alert_per_incident(storage):
    sent = []

    async def notify(text):
        sent.append(text)

    source = Flaky(fail=True)
    service = make_service(storage, source, health=SiteHealth(alert_after_failures=2), notify=notify)
    for _ in range(4):
        with pytest.raises(FetchError):
            await service.search(1, "accountant")
    await asyncio.sleep(0)
    assert len(sent) == 1 and "2 times" in sent[0]

    source.fail = False
    await service.search(1, "accountant")
    await asyncio.sleep(0)
    assert len(sent) == 2 and "working again" in sent[1]


async def test_shared_request_counts_once_for_health(storage):
    gate = asyncio.Event()
    source = FakeSource(fail=True, gate=gate)
    health = SiteHealth(alert_after_failures=2)
    service = make_service(storage, source, health=health)
    waiters = [asyncio.create_task(service.get_jobs("accountant")) for _ in range(3)]
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(*waiters, return_exceptions=True)
    assert not health.is_down("Site A")  # 3 waiters, 1 real failure


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text):
        if chat_id in self.fail_for:
            raise Forbidden("bot was blocked by the user")
        self.sent.append((chat_id, text))


async def test_admin_notifier_sends_to_each_admin_and_survives_errors():
    bot = FakeBot(fail_for={2})
    notifier = AdminNotifier([3, 1, 2])
    await notifier("hello")  # not bound yet: logged only
    notifier.bind(bot)
    await notifier("site down")
    assert bot.sent == [(1, "site down"), (3, "site down")]


async def test_admin_notifier_without_admins_is_silent():
    bot = FakeBot()
    notifier = AdminNotifier([])
    notifier.bind(bot)
    await notifier("x")
    assert bot.sent == []
