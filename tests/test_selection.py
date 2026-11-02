"""Duplicate filtering, stopping at the saved URL per site, and mixing sites."""

from jobbot.job_service import select_new_jobs
from jobbot.models import Job

from .conftest import make_jobs

A, B = "Site A", "Site B"
NEWEST = {A, B}


def urls(jobs: list[Job]) -> list[str]:
    return [j.url for j in jobs]


def select(jobs, sent=(), last=None, newest=NEWEST, limit=10):
    return select_new_jobs(jobs, set(sent), last or {}, newest_first_sources=newest, limit=limit)


def test_skips_jobs_already_sent_to_user():
    jobs = make_jobs(6)
    picked = select(jobs, sent={jobs[1].url, jobs[3].url})
    assert urls(picked) == urls([jobs[0], jobs[2], jobs[4], jobs[5]])


def test_skips_duplicates_within_result_set():
    a, b = make_jobs(2)
    assert urls(select([a, b, a])) == [a.url, b.url]


def test_respects_limit():
    assert len(select(make_jobs(25))) == 10


def test_stops_at_saved_url_when_sorted_newest_first():
    jobs = make_jobs(6)
    # The user last saw jobs[3]; jobs[0..2] are newer.
    assert urls(select(jobs, last={A: jobs[3].url})) == urls(jobs[:3])


def test_does_not_stop_early_when_order_is_unknown():
    jobs = make_jobs(6)
    picked = select(jobs, sent={jobs[3].url}, last={A: jobs[3].url}, newest=set())
    # Jobs after the saved URL may still be new, so only the sent check applies.
    assert urls(picked) == urls(jobs[:3] + jobs[4:])


def test_saved_url_first_means_nothing_new():
    jobs = make_jobs(4)
    assert select(jobs, last={A: jobs[0].url}) == []


def test_all_sent_returns_empty():
    jobs = make_jobs(3)
    assert select(jobs, sent=set(urls(jobs))) == []


def test_sites_are_interleaved_newest_first():
    a = make_jobs(6, "a", A)
    b = make_jobs(6, "b", B)
    picked = select(a + b, limit=6)
    assert urls(picked) == [a[0].url, b[0].url, a[1].url, b[1].url, a[2].url, b[2].url]


def test_one_site_short_other_fills_the_rest():
    a = make_jobs(2, "a", A)
    b = make_jobs(10, "b", B)
    picked = select(a + b, limit=10)
    assert len(picked) == 10
    assert urls(picked)[:4] == [a[0].url, b[0].url, a[1].url, b[1].url]
    assert urls(picked)[4:] == urls(b[2:8])


def test_early_stop_is_per_site():
    a = make_jobs(5, "a", A)
    b = make_jobs(5, "b", B)
    # Saved URL for A is a[1]; B has no saved URL. A's stop must not affect B.
    picked = select(a + b, last={A: a[1].url}, limit=10)
    assert urls(picked) == [a[0].url, b[0].url, b[1].url, b[2].url, b[3].url, b[4].url]


def test_saved_url_of_other_site_is_ignored():
    a = make_jobs(3, "a", A)
    b = make_jobs(3, "b", B)
    picked = select(a + b, last={A: b[0].url})  # a URL that never appears in A's list
    assert len(picked) == 6
