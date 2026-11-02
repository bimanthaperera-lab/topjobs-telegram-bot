import asyncio

import pytest

from jobbot.job_service import JobSearchService
from jobbot.models import Job
from jobbot.storage import Storage


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def make_jobs(n: int, prefix: str = "job", source: str | None = "Site A") -> list[Job]:
    return [Job(url=f"https://example.com/{prefix}/{i}", title=f"{prefix} {i}", source=source) for i in range(n)]


class FakeSource:
    """In-memory JobSource. `jobs` may be a list or a callable(keyword, call_no) -> list."""

    def __init__(self, name="Site A", jobs=None, *, newest_first=True, fail=False, gate: asyncio.Event | None = None):
        self.name = name
        self.sorted_newest_first = newest_first
        self.jobs = jobs if jobs is not None else make_jobs(30, prefix=name.replace(" ", ""), source=name)
        self.fail = fail
        self.gate = gate
        self.calls = 0

    async def fetch(self, keyword, limit):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            from jobbot.sources import SourceError

            raise SourceError(f"{self.name} is down")
        jobs = self.jobs(keyword, self.calls) if callable(self.jobs) else self.jobs
        return list(jobs)[:limit]


def make_service(storage, *sources, **kwargs) -> JobSearchService:
    kwargs.setdefault("cache_ttl_secs", 3600)
    return JobSearchService(storage, list(sources) or [FakeSource()], **kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def storage(clock: FakeClock):
    s = Storage(":memory:", clock=clock)
    yield s
    s.close()
