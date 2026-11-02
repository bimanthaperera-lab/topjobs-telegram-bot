"""Scheduled channel posts: 5 IT jobs per site, new jobs first, then the pool; nothing posted twice."""

import json
from types import SimpleNamespace

import pytest

from jobbot.formatting import format_job
from jobbot.models import Job
from jobbot.notify import (
    XPRESSJOBS_KEYWORDS,
    NotifySettings,
    TopJobsCategories,
    is_it_job,
    load_state,
    run,
    save_state,
)

T0 = 1_800_000_000.0
HOUR = 3600


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Software Engineer - .NET", True),
        ("Senior Network Engineer", True),
        ("Associate Engineer | Service Technician - IT", True),
        ("Senior Project Manager", True),  # listed in an IT category
        ("QA Interns", True),
        ("Sales Executive - IT Products", False),
        ("Intern - Content Developer", False),
        ("Graphic Designer", False),
        ("Human Resources Executive", False),
        ("Accountant", False),
    ],
)
def test_it_category_filter(title, expected):
    assert is_it_job(title) is expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Principal Engineer cum Team Lead (AI, Software)", True),
        ("ML Engineer", True),
        ("Associate UI/UX Engineer", True),
        ("Quality Assurance Engineer", True),
        ("Senior Lead - DevOps", True),
        ("Network Administrator", True),
        ("System Administrator", True),
        ("Service Desk Analyst - French", True),
        ("Help Desk Technician - Tamil Speaking", True),
        ("Desktop Support Engineer", True),
        ("IT Executive", True),
        ("Civil Engineer", False),
        ("Material Engineer | Seethawaka", False),
        ("Procurement Engineer", False),
        ("Quality Assurance Executive", False),
        ("Technical Sales Engineer | Nawala", False),
        ("Security Officer", False),
        ("AC Technician", False),
        ("Office Administrator", False),
        ("CRM Assistant - Bpo Experience", False),
    ],
)
def test_site_wide_listing_needs_an_it_title(title, expected):
    assert is_it_job(title, site_wide=True) is expected


def test_tag_is_the_first_line():
    text = format_job(Job(url="https://x.lk/1", title="Dev <1>"), tag="🆕 New Match")
    assert text.startswith("<b>🆕 New Match</b>\n<b>Dev &lt;1&gt;</b>")


def tj(n, title="Software Engineer"):
    return Job(url=f"https://x.lk/{n}", title=f"{title} {n}", source="TopJobs.lk")


def xj(n, title="Software Engineer"):
    return Job(url=f"https://xpress.jobs/jobs/view/{n}", title=f"{title} X{n}", source="XpressJobs")


def newest_first(make, hi, lo=1):
    return [make(n) for n in range(hi, lo - 1, -1)]


class FakeTopJobs:
    name = "TopJobs.lk"

    def __init__(self, jobs):
        self.jobs = jobs

    async def fetch(self, keyword, limit):
        return list(self.jobs)

    async def fetch_image(self, url):
        return None  # text posts keep these tests simple


class FakeXpress:
    """Like XpressJobsSource: fetch_newest, and no ad images."""

    name = "XpressJobs"

    def __init__(self, jobs=None, error=None):
        self.jobs = jobs or []
        self.error = error
        self.keywords = None

    async def fetch(self, keyword, limit):
        raise AssertionError("the channel posts use fetch_newest")

    async def fetch_newest(self, keywords):
        self.keywords = keywords
        if self.error:
            raise self.error
        return list(self.jobs)


class ChannelBot:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            from telegram.error import Forbidden

            raise Forbidden("bot is not a member of the channel chat")
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, **kwargs))

    def posts(self):
        """[(title, tagged new)] in posting order."""
        out = []
        for m in self.sent:
            lines = m.text.split("\n")
            tagged = lines[0] == "<b>🆕 New Match</b>"
            out.append((lines[1 if tagged else 0][3:-4], tagged))
        return out

    def titles(self):
        return [t for t, _ in self.posts()]


def settings(tmp_path, **overrides):
    return NotifySettings(bot_token="x", chat_id="@itjobs", state_file=str(tmp_path / "seen.json"), **overrides)


async def post(s, sources, now, **bot_kwargs):
    bot = ChannelBot(**bot_kwargs)
    status = await run(s, sources, bot, now=now)
    return bot, status


def se(*numbers, x=False):
    return [f"Software Engineer {'X' if x else ''}{n}" for n in numbers]


async def test_first_run_posts_the_newest_five_untagged(tmp_path):
    s = settings(tmp_path)
    bot, status = await post(s, [FakeTopJobs(newest_first(tj, 8) + [tj(99, "Sales Executive")])], T0)
    assert status == 0
    assert bot.posts() == [(t, False) for t in se(8, 7, 6, 5, 4)]
    assert all(m.chat_id == "@itjobs" for m in bot.sent)
    saved = json.load(open(s.state_file, encoding="utf-8"))
    assert saved["version"] == 3 and "https://x.lk/99" not in saved["first_seen"]


async def test_five_from_each_site(tmp_path):
    s = settings(tmp_path)
    xpress = FakeXpress(newest_first(xj, 15))
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 15)), xpress], T0)
    assert xpress.keywords == XPRESSJOBS_KEYWORDS
    assert bot.titles()[:4] == ["Software Engineer 15", "Software Engineer X15", "Software Engineer 14",
                                "Software Engineer X14"]
    assert sorted(bot.titles()) == sorted(se(15, 14, 13, 12, 11) + se(15, 14, 13, 12, 11, x=True))


async def test_pool_is_ten_from_each_site(tmp_path):
    s = settings(tmp_path, per_site=50)
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 15)), FakeXpress(newest_first(xj, 15))], T0)
    assert sorted(bot.titles()) == sorted(se(*range(15, 5, -1)) + se(*range(15, 5, -1), x=True))


async def test_new_jobs_come_first_with_the_tag(tmp_path):
    s = settings(tmp_path)
    await post(s, [FakeTopJobs(newest_first(tj, 10))], T0)  # posts 10..6
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 11))], T0 + 6 * HOUR)
    assert bot.posts() == [("Software Engineer 11", True)] + [(t, False) for t in se(5, 4, 3, 2)]


async def test_every_sites_new_jobs_go_before_pool_jobs(tmp_path):
    s = settings(tmp_path)
    await post(s, [FakeTopJobs(newest_first(tj, 10)), FakeXpress(newest_first(xj, 10))], T0)
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 11)), FakeXpress(newest_first(xj, 11))], T0 + 6 * HOUR)
    assert bot.posts()[:2] == [("Software Engineer 11", True), ("Software Engineer X11", True)]
    assert not any(tagged for _, tagged in bot.posts()[2:])
    assert len(bot.sent) == 10


async def test_each_site_keeps_its_own_quota(tmp_path):
    s = settings(tmp_path)
    await post(s, [FakeTopJobs([tj(1)]), FakeXpress(newest_first(xj, 8))], T0)
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 8)), FakeXpress(newest_first(xj, 8))], T0 + HOUR)
    topjobs = [p for p in bot.posts() if "X" not in p[0]]
    xpress = [p for p in bot.posts() if "X" in p[0]]
    assert topjobs == [(t, True) for t in se(8, 7, 6, 5, 4)]  # 7 new: the newest 5 now, the rest next run
    assert xpress == [(t, False) for t in se(3, 2, 1, x=True)]  # 8..4 went out an hour ago


async def test_posted_jobs_are_never_posted_again(tmp_path):
    s = settings(tmp_path)
    site = [FakeTopJobs(newest_first(tj, 25))]  # one site: the pool is its newest 20 (25..6)
    rounds = []
    for hours in (0, 6, 12, 18):
        bot, _ = await post(s, site, T0 + hours * HOUR)
        rounds.append(bot.titles())
    assert [r[0] for r in rounds] == se(25, 20, 15, 10)
    assert sum(len(r) for r in rounds) == 20 and len(set(sum(rounds, []))) == 20

    for hours in (20, 24, 72):
        quiet, _ = await post(s, site, T0 + hours * HOUR)
        assert quiet.sent == []  # the pool was all posted, nothing new: send nothing

    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 26))], T0 + 73 * HOUR)
    assert bot.posts() == [("Software Engineer 26", True)]  # only the new one


async def test_reposting_can_be_switched_on(tmp_path):
    s = settings(tmp_path, repost_hours=48)
    site = [FakeTopJobs(newest_first(tj, 5))]
    await post(s, site, T0)
    bot, _ = await post(s, site, T0 + 24 * HOUR)
    assert bot.sent == []
    bot, _ = await post(s, site, T0 + 48 * HOUR)
    assert bot.titles() == se(5, 4, 3, 2, 1)


def test_repost_setting_is_off_unless_set(monkeypatch):
    from jobbot.notify import load_notify_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("NOTIFY_CHAT_ID", "@c")
    monkeypatch.setenv("NOTIFY_REPOST_HOURS", "")
    assert load_notify_settings().repost_hours is None
    monkeypatch.setenv("NOTIFY_REPOST_HOURS", "48")
    assert load_notify_settings().repost_hours == 48


async def test_new_jobs_beyond_the_quota_wait_for_the_next_run(tmp_path):
    s = settings(tmp_path, per_site=2)
    await post(s, [FakeTopJobs([tj(1)])], T0)
    site = [FakeTopJobs(newest_first(tj, 4))]
    first, _ = await post(s, site, T0 + HOUR)
    second, _ = await post(s, site, T0 + 2 * HOUR)
    assert first.posts() == [("Software Engineer 4", True), ("Software Engineer 3", True)]
    assert second.posts() == [("Software Engineer 2", True)]  # job 1 was already posted


async def test_new_tag_expires_after_48_hours(tmp_path):
    s = settings(tmp_path, per_site=1)
    await post(s, [FakeTopJobs([tj(1)])], T0)
    await post(s, [FakeTopJobs(newest_first(tj, 3))], T0 + HOUR)  # 3 posted as new; 2 waits
    bot, _ = await post(settings(tmp_path, per_site=3), [FakeTopJobs(newest_first(tj, 3))], T0 + 50 * HOUR)
    assert bot.posts() == [("Software Engineer 2", False)]  # untagged now; 3 and 1 went out before


async def test_site_switched_on_later_is_not_tagged_new(tmp_path):
    s = settings(tmp_path)
    await post(s, [FakeTopJobs(newest_first(tj, 3))], T0)
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 3)), FakeXpress([xj(9), xj(8)])], T0 + HOUR)
    assert bot.posts() == [(t, False) for t in se(9, 8, x=True)]

    later, _ = await post(s, [FakeTopJobs(newest_first(tj, 3)), FakeXpress(newest_first(xj, 10, 8))], T0 + 2 * HOUR)
    assert later.posts() == [("Software Engineer X10", True)]


async def test_changing_what_is_tracked_does_not_tag_old_jobs(tmp_path):
    s = settings(tmp_path)
    await post(s, [FakeTopJobs([tj(1)])], T0)
    state = load_state(s.state_file, T0)
    state.scope = "an older filter"
    save_state(s.state_file, state)
    bot, _ = await post(s, [FakeTopJobs(newest_first(tj, 3))], T0 + HOUR)  # 3 and 2 now match too
    assert bot.posts() == [(t, False) for t in se(3, 2)]


async def test_oldest_state_file_is_upgraded(tmp_path):
    s = settings(tmp_path)
    with open(s.state_file, "w", encoding="utf-8") as fh:
        json.dump(["https://x.lk/1"], fh)  # the oldest format: URLs already posted
    bot, _ = await post(s, [FakeTopJobs([tj(2), tj(1)])], T0)
    assert bot.posts() == [("Software Engineer 2", False)]  # 1 was posted under the old format
    assert load_state(s.state_file, T0).posted == {"https://x.lk/2": T0, "https://x.lk/1": 0.0}


async def test_duplicate_listings_are_posted_once(tmp_path):
    s = settings(tmp_path)
    jobs = [Job(url="https://xpress.jobs/jobs/view/2", title="Azure Admin | Hybrid", company="ACME"),
            Job(url="https://xpress.jobs/jobs/view/1", title="Azure Admin", company="ACME")]
    bot, _ = await post(s, [FakeXpress(jobs)], T0)
    assert bot.titles() == ["Azure Admin | Hybrid"]


async def test_topjobs_categories_are_one_site():
    class Category:
        name = "TopJobs.lk"

        def __init__(self, jobs):
            self.jobs = jobs

        async def fetch(self, keyword, limit):
            return list(self.jobs)

    site = TopJobsCategories(http=None, categories=())
    site._sources = [Category([tj(5), tj(3)]), Category([tj(4), tj(3)])]
    assert site.name == "TopJobs.lk"
    assert [j.url for j in await site.fetch("", 500)] == ["https://x.lk/5", "https://x.lk/4", "https://x.lk/3"]


async def test_one_site_failing_still_posts_the_other(tmp_path):
    from jobbot.sources import SourceError

    s = settings(tmp_path)
    bot, status = await post(s, [FakeTopJobs([tj(2), tj(1)]), FakeXpress(error=SourceError("xpress.jobs: HTTP 503"))], T0)
    assert status == 1  # the run shows red so the failure gets noticed
    assert bot.titles() == se(2, 1)


async def test_every_site_failing_raises(tmp_path):
    from jobbot.sources import SourceError

    with pytest.raises(SourceError):
        await post(settings(tmp_path), [FakeXpress(error=SourceError("down"))], T0)


async def test_partial_failure_keeps_what_was_posted(tmp_path):
    from telegram.error import Forbidden

    s = settings(tmp_path)
    with pytest.raises(Forbidden):
        await post(s, [FakeTopJobs(newest_first(tj, 5))], T0, fail_after=2)
    assert set(load_state(s.state_file, T0).posted) == {"https://x.lk/5", "https://x.lk/4"}


async def test_gone_jobs_are_forgotten_after_a_month(tmp_path):
    s = settings(tmp_path)
    await post(s, [FakeTopJobs([tj(2), tj(1)])], T0)
    await post(s, [FakeTopJobs([tj(2)])], T0 + 10 * 24 * HOUR)
    assert "https://x.lk/1" in load_state(s.state_file, T0).first_seen  # a site glitch doesn't reset it
    await post(s, [FakeTopJobs([tj(2)])], T0 + 31 * 24 * HOUR)
    state = load_state(s.state_file, T0)
    assert "https://x.lk/1" not in state.first_seen and "https://x.lk/1" not in state.posted
