import logging

from jobbot.formatting import DESCRIPTION_MAX, TELEGRAM_MAX_MESSAGE, format_job, format_jobs, telegram_length
from jobbot.logging_setup import RedactingFormatter
from jobbot.models import Job

SAMPLE = Job(
    url="https://www.topjobs.lk/employer/JobAdvertismentServlet?rid=0&ac=DEFZZZ&jc=0001541016&ec=DEFZZZ",
    title="Assistant Accountant / Senior Finance Executive",
    company="Sentab Groups",
    location="Piliyandala",
    posted="Mon Nov 02 2026",
    closing="Mon Nov 16 2026",
    description="Overseas opportunity",
    source="TopJobs.lk",
)


def test_all_fields_shown():
    text = format_job(SAMPLE, 1)
    assert text.startswith("<b>1. Assistant Accountant")
    for part in ("🏢 Sentab Groups", "📍 Piliyandala", "📅 Posted: Mon Nov 02 2026",
                 "⏳ Closes: Mon Nov 16 2026", "📝 Overseas opportunity", ">View on TopJobs.lk</a>"):
        assert part in text


def test_missing_fields_are_left_out():
    text = format_job(Job(url="https://x.lk/1", title="Driver"))
    for marker in ("🏢", "📍", "📅", "⏳", "📝"):
        assert marker not in text
    assert "Driver" in text and 'href="https://x.lk/1">View job</a>' in text


def test_scraped_text_is_escaped():
    job = Job(
        url='https://x.lk/?a=1&b="2"',
        title="<script>alert(1)</script>",
        company="A & B <Ltd>",
        description="<b>bold</b>",
        source="<Site>",
    )
    text = format_job(job)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "A &amp; B &lt;Ltd&gt;" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
    assert "View on &lt;Site&gt;" in text
    assert 'href="https://x.lk/?a=1&amp;b=&quot;2&quot;"' in text


def test_description_is_truncated():
    text = format_job(Job(url="https://x.lk/1", title="T", description="x" * 500))
    line = next(l for l in text.splitlines() if l.startswith("📝"))
    assert len(line[2:]) == DESCRIPTION_MAX
    assert line.endswith("…")


def test_messages_stay_under_telegram_limit():
    long = "<&>" * 2000
    jobs = [Job(url=f"https://x.lk/{i}", title=long, company=long, location=long, posted=long,
                closing=long, description=long, source="TopJobs.lk") for i in range(10)]
    pages = format_jobs(jobs, header="Found jobs")
    assert pages
    assert all(telegram_length(p) <= TELEGRAM_MAX_MESSAGE for p in pages)


def test_five_normal_jobs_fit_in_one_message():
    assert len(format_jobs([SAMPLE] * 5, header="Found jobs")) == 1


def test_log_formatter_redacts_tokens():
    bot_token = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"
    fmt = RedactingFormatter("%(message)s", [bot_token])
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "POST https://api.telegram.org/bot%s/getMe", (bot_token,), None)
    out = fmt.format(record)
    assert bot_token not in out
    assert "***" in out


def test_log_formatter_redacts_unknown_token_shapes():
    fmt = RedactingFormatter("%(message)s", [])
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "url=/bot987654321:ZZZyyyXXXwwwVVVuuuTTTsssRRRqqqPPP/", None, None)
    assert "987654321:" not in fmt.format(record)
