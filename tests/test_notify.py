"""Scheduled channel alerts for new software engineering jobs."""

import json
from types import SimpleNamespace

import pytest

from jobbot.models import Job
from jobbot.notify import NotifySettings, is_software_job, load_seen, pick_new, run, save_seen


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior Software Engineer - .NET", True),
        ("Backend Developer", True),
        ("Full - Stack Engineer - Java & React", True),
        ("DevOps Engineer", True),
        ("QA Interns", True),
        ("Tech Lead - AI", True),
        ("Graphic Designer", False),
        ("Senior Project Manager", False),
        ("Sales Executive - IT Products", False),
        ("Intern - Content Developer", False),
        ("Senior Network Engineer", False),
        ("Associate Engineer | Service Technician - IT", False),
    ],
)
def test_software_job_filter(title, expected):
    assert is_software_job(title) is expected


def job(n, title="Software Engineer"):
    return Job(url=f"https://x.lk/{n}", title=f"{title} {n}", source="TopJobs.lk")


def test_pick_new_oldest_first_with_overflow():
    jobs = [job(5), job(4), Job(url="https://x.lk/d", title="Designer"), job(3), job(2), job(1)]  # newest first
    to_post, overflow = pick_new(jobs, seen=["https://x.lk/1"], limit=2)
    assert [j.url for j in to_post] == ["https://x.lk/4", "https://x.lk/5"]  # newest ends up last
    assert [j.url for j in overflow] == ["https://x.lk/3", "https://x.lk/2"]


def test_state_file_round_trip(tmp_path):
    path = str(tmp_path / "state" / "seen.json")
    assert load_seen(path) is None
    save_seen(path, ["a", "b"])
    assert load_seen(path) == ["a", "b"]


class FakeSourceForNotify:
    def __init__(self, jobs):
        self.jobs = jobs
        self.name = "TopJobs.lk"

    async def fetch(self, keyword, limit):
        return list(self.jobs)

    async def fetch_image(self, url):
        return None  # text posts keep this test simple


class ChannelBot:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            from telegram.error import Forbidden

            raise Forbidden("bot is not a member of the channel chat")
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, **kwargs))


def settings(tmp_path, max_per_run=15):
    return NotifySettings(bot_token="x", chat_id="@itjobs", state_file=str(tmp_path / "seen.json"),
                          max_per_run=max_per_run, send_images=True)


async def test_first_run_only_records_and_announces(tmp_path):
    s = settings(tmp_path)
    bot = ChannelBot()
    await run(s, FakeSourceForNotify([job(2), job(1), Job(url="https://x.lk/d", title="Designer")]), bot)
    assert len(bot.sent) == 1 and "alerts are on" in bot.sent[0].text and "2 current" in bot.sent[0].text
    assert sorted(json.load(open(s.state_file))) == ["https://x.lk/1", "https://x.lk/2"]


async def test_posts_only_new_jobs_then_nothing(tmp_path):
    s = settings(tmp_path)
    save_seen(s.state_file, ["https://x.lk/1"])
    bot = ChannelBot()
    await run(s, FakeSourceForNotify([job(3), job(2), job(1)]), bot)
    assert "2 new software engineering jobs" in bot.sent[0].text
    assert [m.text.split("</b>")[0] for m in bot.sent[1:]] == ["<b>Software Engineer 2", "<b>Software Engineer 3"]
    assert all(m.chat_id == "@itjobs" for m in bot.sent)

    again = ChannelBot()
    await run(s, FakeSourceForNotify([job(3), job(2), job(1)]), again)
    assert again.sent == []


async def test_overflow_is_skipped_and_mentioned(tmp_path):
    s = settings(tmp_path, max_per_run=1)
    save_seen(s.state_file, [])
    bot = ChannelBot()
    await run(s, FakeSourceForNotify([job(3), job(2), job(1)]), bot)
    assert "3 new" in bot.sent[0].text and "newest 1" in bot.sent[0].text
    assert len(bot.sent) == 2
    assert set(load_seen(s.state_file)) == {"https://x.lk/1", "https://x.lk/2", "https://x.lk/3"}


async def test_partial_failure_keeps_what_was_posted(tmp_path):
    from telegram.error import Forbidden

    s = settings(tmp_path)
    save_seen(s.state_file, [])
    bot = ChannelBot(fail_after=2)  # header + first job succeed, then the channel refuses
    with pytest.raises(Forbidden):
        await run(s, FakeSourceForNotify([job(3), job(2), job(1)]), bot)
    assert load_seen(s.state_file) == ["https://x.lk/1"]  # only the posted one; others retried next run
