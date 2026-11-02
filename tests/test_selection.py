"""Duplicate filtering and stopping at the saved most-recent URL."""

from jobbot.job_service import select_new_jobs
from jobbot.models import Job

from .conftest import make_jobs


def urls(jobs: list[Job]) -> list[str]:
    return [j.url for j in jobs]


def test_skips_jobs_already_sent_to_user():
    jobs = make_jobs(6)
    sent = {jobs[1].url, jobs[3].url}
    picked = select_new_jobs(jobs, sent, None, sorted_newest_first=False, limit=10)
    assert urls(picked) == urls([jobs[0], jobs[2], jobs[4], jobs[5]])


def test_skips_duplicates_within_result_set():
    a, b = make_jobs(2)
    picked = select_new_jobs([a, b, a], set(), None, sorted_newest_first=False, limit=10)
    assert urls(picked) == [a.url, b.url]


def test_respects_limit():
    picked = select_new_jobs(make_jobs(25), set(), None, sorted_newest_first=False, limit=10)
    assert len(picked) == 10


def test_stops_at_saved_url_when_sorted_newest_first():
    jobs = make_jobs(6)
    # The user last saw jobs[3]; jobs[0..2] are newer.
    picked = select_new_jobs(jobs, set(), jobs[3].url, sorted_newest_first=True, limit=10)
    assert urls(picked) == urls(jobs[:3])


def test_does_not_stop_early_when_order_is_unknown():
    jobs = make_jobs(6)
    sent = {jobs[3].url}
    picked = select_new_jobs(jobs, sent, jobs[3].url, sorted_newest_first=False, limit=10)
    # Jobs after the saved URL may still be new, so only the sent check applies.
    assert urls(picked) == urls(jobs[:3] + jobs[4:])


def test_saved_url_first_means_nothing_new():
    jobs = make_jobs(4)
    picked = select_new_jobs(jobs, set(), jobs[0].url, sorted_newest_first=True, limit=10)
    assert picked == []


def test_all_sent_returns_empty():
    jobs = make_jobs(3)
    picked = select_new_jobs(jobs, set(urls(jobs)), None, sorted_newest_first=False, limit=10)
    assert picked == []
