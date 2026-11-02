import pytest

from jobbot.models import Job
from jobbot.storage import Storage


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def make_jobs(n: int, prefix: str = "job") -> list[Job]:
    return [Job(url=f"https://example.com/{prefix}/{i}", title=f"{prefix} {i}") for i in range(n)]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def storage(clock: FakeClock):
    s = Storage(":memory:", clock=clock)
    yield s
    s.close()
