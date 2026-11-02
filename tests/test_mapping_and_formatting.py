import logging

from jobbot.actor_mapping import build_run_input, map_item
from jobbot.formatting import DESCRIPTION_MAX, TELEGRAM_MAX_MESSAGE, format_job, format_jobs, telegram_length
from jobbot.logging_setup import RedactingFormatter
from jobbot.models import Job

DETAIL_URL = (
    "https://topjobs.lk/employer/JobAdvertismentServlet?rid=0&ac=DEFZZZ&jc=0001541016"
    "&ec=DEFZZZ&pg=applicant/vacancybyfunctionalarea.jsp"
)

FULL_ITEM = {
    "_input": "keyword:accountant",
    "_source": "S1-detail-html",
    "position": "Assistant Accountant / Senior Finance Executive",
    "employer": "Sentab Groups",
    "location": "Piliyandala, Sri Lanka",
    "closingDate": "08/09/2026",
    "jobType": ["Full Time", "Permanent", "Onsite"],
    "jsonLd": {
        "description": "Please refer the full details of the job posting as given by the employer. </br>...",
        "datePosted": "2026-08-26",
    },
    "detailUrl": DETAIL_URL,
}

COMPACT_ITEM = {
    "position": "Sales Associate",
    "employer": "L.K.Group Holdings Pvt Ltd",
    "town": "Nugegoda",
    "openingDate": "Wed Aug 26 2026",
    "closingDate": "Wed Sep 09 2026",
    "detailUrl": DETAIL_URL,
}


def test_run_input_requests_only_needed_items():
    assert build_run_input("accountant", 10, True) == {
        "mode": "search",
        "keyword": "accountant",
        "maxResults": 10,
        "fetchFullDetails": True,
    }


def test_map_full_detail_item():
    job = map_item(FULL_ITEM)
    assert job.title == "Assistant Accountant / Senior Finance Executive"
    assert job.company == "Sentab Groups"
    assert job.location == "Piliyandala, Sri Lanka"
    assert job.posted == "2026-08-26"
    # Boilerplate jsonLd text is dropped in favour of badges + closing date.
    assert job.description == "Full Time · Permanent · Onsite. Closes 08/09/2026"
    assert job.url == DETAIL_URL


def test_map_compact_item():
    job = map_item(COMPACT_ITEM)
    assert job.location == "Nugegoda"
    assert job.posted == "Wed Aug 26 2026"
    assert job.description == "Closes Wed Sep 09 2026"


def test_map_uses_real_json_ld_description():
    item = dict(FULL_ITEM, jsonLd={"description": "<p>Prepare &amp; review accounts</p>"})
    assert map_item(item).description == "Prepare & review accounts"


def test_map_skips_errors_and_incomplete_items():
    assert map_item({"_error": "not_found", "detailUrl": DETAIL_URL}) is None
    assert map_item({"position": "No link"}) is None
    assert map_item({"detailUrl": DETAIL_URL}) is None
    assert map_item({"position": "Bad link", "detailUrl": "javascript:alert(1)"}) is None
    assert map_item("not a dict") is None


def test_missing_fields_are_left_out():
    text = format_job(Job(url="https://x.lk/1", title="Driver"))
    assert "🏢" not in text and "📍" not in text and "📅" not in text and "📝" not in text
    assert "Driver" in text and 'href="https://x.lk/1"' in text


def test_scraped_text_is_escaped():
    job = Job(
        url='https://x.lk/?a=1&b="2"',
        title="<script>alert(1)</script>",
        company="A & B <Ltd>",
        description="<b>bold</b>",
    )
    text = format_job(job)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "A &amp; B &lt;Ltd&gt;" in text
    assert "&lt;b&gt;bold&lt;/b&gt;" in text
    assert 'href="https://x.lk/?a=1&amp;b=&quot;2&quot;"' in text


def test_description_is_truncated():
    text = format_job(Job(url="https://x.lk/1", title="T", description="x" * 500))
    line = next(l for l in text.splitlines() if l.startswith("📝"))
    assert len(line[2:]) == DESCRIPTION_MAX
    assert line.endswith("…")


def test_messages_stay_under_telegram_limit():
    long = "<&>" * 2000
    jobs = [Job(url=f"https://x.lk/{i}", title=long, company=long, location=long, posted=long, description=long)
            for i in range(10)]
    pages = format_jobs(jobs, header="Found jobs")
    assert pages
    assert all(telegram_length(p) <= TELEGRAM_MAX_MESSAGE for p in pages)


def test_five_normal_jobs_fit_in_one_message():
    job = map_item(FULL_ITEM)
    assert len(format_jobs([job] * 5, header="Found jobs")) == 1


def test_log_formatter_redacts_tokens():
    bot_token = "123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"
    fmt = RedactingFormatter("%(message)s", [bot_token, "apify_api_SECRET"])
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "POST https://api.telegram.org/bot%s/getMe token=%s", (bot_token, "apify_api_SECRET"), None)
    out = fmt.format(record)
    assert bot_token not in out and "apify_api_SECRET" not in out
    assert "***" in out


def test_log_formatter_redacts_unknown_token_shapes():
    fmt = RedactingFormatter("%(message)s", [])
    record = logging.LogRecord("x", logging.INFO, __file__, 1,
                               "url=/bot987654321:ZZZyyyXXXwwwVVVuuuTTTsssRRRqqqPPP/ and apify_api_abc123", None, None)
    out = fmt.format(record)
    assert "987654321:" not in out and "apify_api_abc123" not in out
