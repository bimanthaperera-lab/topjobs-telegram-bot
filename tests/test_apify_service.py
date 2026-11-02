"""ApifyJobFetcher against a fake async client (no network)."""

from types import SimpleNamespace

import pytest

from jobbot.apify_service import ActorRunError, ApifyJobFetcher, is_low_memory_failure, pick_memory

URL = "https://topjobs.lk/employer/JobAdvertismentServlet?jc={}"


def run(status="SUCCEEDED", message=None, exit_code=0, dataset="ds1", run_id="r1"):
    return SimpleNamespace(id=run_id, status=status, status_message=message, exit_code=exit_code,
                           default_dataset_id=dataset)


class FakeDataset:
    def __init__(self, items):
        self.items = items
        self.kwargs = None

    def iterate_items(self, **kwargs):
        self.kwargs = kwargs
        items = self.items[: kwargs.get("limit") or None]

        async def gen():
            for item in items:
                yield item

        return gen()


class FakeClient:
    def __init__(self, runs, items, min_memory=None):
        self.runs = list(runs)
        self.calls = []
        self.dataset_obj = FakeDataset(items)
        self.min_memory = min_memory
        self.aborted = []

    def actor(self, actor_id):
        client = self

        class Actor:
            async def get(self):
                return SimpleNamespace(tagged_builds={"latest": SimpleNamespace(build_id="b1")})

            async def call(self, **kwargs):
                client.calls.append(kwargs)
                return client.runs.pop(0)

        return Actor()

    def build(self, build_id):
        client = self

        class Build:
            async def get(self):
                return SimpleNamespace(actor_definition=SimpleNamespace(min_memory_mbytes=client.min_memory))

        return Build()

    def dataset(self, dataset_id):
        return self.dataset_obj

    def run(self, run_id):
        client = self

        class Run:
            async def abort(self):
                client.aborted.append(run_id)

        return Run()


def items(n):
    return [{"position": f"Job {i}", "detailUrl": URL.format(i)} for i in range(n)]


def fetcher(client, **kw):
    opts = dict(memory_mb=256, fallback_memory_mb=1024, timeout_secs=120, fetch_full_details=True)
    opts.update(kw)
    return ApifyJobFetcher(client, "fanndev/topjobs-lk-scraper", **opts)


def test_pick_memory():
    assert pick_memory(256, None) == 256
    assert pick_memory(256, 128) == 256
    assert pick_memory(256, 512) == 512


def test_low_memory_detection():
    assert is_low_memory_failure("FAILED", "The Actor ran out of memory", 1)
    assert is_low_memory_failure("FAILED", None, 137)
    assert not is_low_memory_failure("FAILED", "Proxy error", 1)
    assert not is_low_memory_failure("SUCCEEDED", "memory", 0)


async def test_run_uses_cost_limits():
    client = FakeClient([run()], items(10))
    jobs = await fetcher(client).fetch("accountant", 10)
    assert len(jobs) == 10
    call = client.calls[0]
    assert call["memory_mbytes"] == 256
    assert call["max_items"] == 10
    assert call["run_input"]["maxResults"] == 10
    assert call["run_input"]["keyword"] == "accountant"
    assert call["run_timeout"].total_seconds() == 120
    assert client.dataset_obj.kwargs["limit"] == 10


async def test_actor_minimum_memory_wins():
    client = FakeClient([run()], items(1), min_memory=512)
    await fetcher(client).fetch("accountant", 10)
    assert client.calls[0]["memory_mbytes"] == 512


async def test_retries_once_with_fallback_on_low_memory():
    client = FakeClient([run("FAILED", "ran out of memory", run_id="r1"), run(run_id="r2")], items(3))
    jobs = await fetcher(client).fetch("accountant", 10)
    assert [c["memory_mbytes"] for c in client.calls] == [256, 1024]
    assert len(jobs) == 3


async def test_does_not_retry_other_failures():
    client = FakeClient([run("FAILED", "blocked by site")], [])
    with pytest.raises(ActorRunError):
        await fetcher(client).fetch("accountant", 10)
    assert len(client.calls) == 1


async def test_timed_out_run_uses_partial_results():
    client = FakeClient([run("TIMED-OUT")], items(4))
    jobs = await fetcher(client).fetch("accountant", 10)
    assert len(jobs) == 4


async def test_still_running_run_is_aborted():
    client = FakeClient([run("RUNNING")], [])
    with pytest.raises(ActorRunError):
        await fetcher(client).fetch("accountant", 10)
    assert client.aborted == ["r1"]


async def test_bad_items_are_skipped():
    data = items(2) + [{"_error": "not_found"}, {"position": "no url"}, items(1)[0]]
    client = FakeClient([run()], data)
    jobs = await fetcher(client).fetch("accountant", 10)
    assert [j.title for j in jobs] == ["Job 0", "Job 1"]  # duplicate of Job 0 dropped
