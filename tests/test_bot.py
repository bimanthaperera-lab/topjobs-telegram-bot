"""Bot wiring, message helpers and handler flow (fake Telegram objects, no network)."""

from types import SimpleNamespace

from telegram.error import BadRequest

from jobbot.bot import (
    CALLBACK_PREFIX,
    CATEGORY_PREFIX,
    JOB_BUTTON_LABEL,
    SHOW_MORE_LABEL,
    _limit_message,
    _source_notice,
    build_application,
    categories,
    category_keyboard,
    choose_category,
    job_keyboard,
    search,
    send_job,
    send_jobs,
    show_more,
    start,
)
from jobbot.categories import CATEGORIES, category_by_id
from jobbot.formatting import format_job
from jobbot.models import Job
from jobbot.normalize import normalize_keyword
from jobbot.rate_limit import REASON_COOLDOWN, REASON_USER_FETCHES, Decision

from .conftest import FakeSource, make_service


def test_application_builds_with_handlers(storage):
    app = build_application("123456:" + "A" * 35, make_service(storage, FakeSource()))
    names = sorted(type(h).__name__ for h in app.handlers[0])
    assert names == ["CallbackQueryHandler", "CallbackQueryHandler", "CommandHandler", "CommandHandler",
                     "MessageHandler"]


def test_show_more_label_and_callback_size():
    assert SHOW_MORE_LABEL == "Thawa Jobs Pennanna (Show More)"
    assert len((CALLBACK_PREFIX + "x" * 11).encode()) < 64


def test_source_notice():
    assert _source_notice([]) is None
    notice = _source_notice(["XpressJobs"])
    assert "XpressJobs" in notice and "incomplete" in notice
    assert "&lt;b&gt;" in _source_notice(["<b>"])  # escaped
    stale = _source_notice([], ["TopJobs.lk"])
    assert "TopJobs.lk" in stale and "outdated" in stale
    both = _source_notice(["XpressJobs"], ["TopJobs.lk"])
    assert both.count("\n") == 1


def test_limit_messages():
    assert "10 seconds" in _limit_message(Decision(False, REASON_COOLDOWN, 10))
    assert "today's limit" in _limit_message(Decision(False, REASON_USER_FETCHES, 7200))


# --- one message per job, with an inline button --------------------------------

JOB =Job(url="https://www.topjobs.lk/employer/JobAdvertismentServlet?ac=A&jc=1", title="Software Engineer",
          company="Acme", source="TopJobs.lk")


class FakeBot:
    def __init__(self, reject_buttons=False):
        self.sent = []
        self.reject_buttons = reject_buttons

    async def send_message(self, chat_id, text, **kwargs):
        if self.reject_buttons and kwargs.get("reply_markup") is not None and "url" in str(kwargs["reply_markup"]):
            raise BadRequest("Button_url_invalid")
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, **kwargs))

    async def send_chat_action(self, *args, **kwargs):
        pass


def button(msg):
    return msg.reply_markup.inline_keyboard[0][0]


def test_job_keyboard_opens_job_page():
    kb = job_keyboard(JOB)
    btn = kb.inline_keyboard[0][0]
    assert btn.url == JOB.url
    assert btn.text == f"{JOB_BUTTON_LABEL} on TopJobs.lk"


def test_job_keyboard_skips_bad_urls():
    assert job_keyboard(Job(url="javascript:alert(1)", title="x")) is None
    assert job_keyboard(Job(url="https://x.lk/" + "a" * 3000, title="x")) is None


def test_text_has_no_link_when_button_used():
    assert "<a href" not in format_job(JOB, 1, include_link=False)
    assert "<a href" in format_job(JOB, 1, include_link=True)


async def test_send_job_uses_button():
    bot = FakeBot()
    await send_job(bot, 42, JOB, 3)
    (msg,) = bot.sent
    assert msg.text.startswith("<b>3. Software Engineer")
    assert "<a href" not in msg.text
    assert button(msg).url == JOB.url


async def test_send_job_falls_back_to_text_link_if_button_rejected():
    bot = FakeBot(reject_buttons=True)
    await send_job(bot, 42, JOB, 1)
    (msg,) = bot.sent
    assert getattr(msg, "reply_markup", None) is None
    assert "<a href" in msg.text


async def test_send_jobs_one_message_each_numbered():
    bot = FakeBot()
    jobs = [Job(url=f"https://x.lk/{i}", title=f"Job {i}") for i in range(3)]
    await send_jobs(bot, 42, jobs, start_index=6)
    assert [m.text.split("</b>")[0] for m in bot.sent] == ["<b>6. Job 0", "<b>7. Job 1", "<b>8. Job 2"]
    assert all(button(m).url.startswith("https://x.lk/") for m in bot.sent)


# --- full handler flow with fake Telegram objects ------------------------------

class FakeStatus:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.chat_id = 42
        self.status = FakeStatus()
        self.replies = []
        self.reply_markups = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.reply_markups.append(kwargs.get("reply_markup"))
        return self.status


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=42))
        self.answers = []
        self.markup_removed = False

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_removed = True


def fake_context(service, bot):
    app = SimpleNamespace(bot_data={"service": service, "limiter": None})
    return SimpleNamespace(application=app, bot=bot)


async def test_search_then_show_more_flow(storage):
    jobs = [Job(url=f"https://x.lk/{i}", title=f"Driver {i}", source="TopJobs.lk") for i in range(8)]
    service = make_service(storage, FakeSource(jobs=jobs))
    bot = FakeBot()
    ctx = fake_context(service, bot)
    msg = FakeMessage("  Driver ")
    await search(SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=7)), ctx)

    assert msg.replies[0].startswith("🔎 Searching")
    assert msg.status.edits == ["🔎 Found 8 new jobs for <b>driver</b>:"]  # header replaces "Searching..."
    job_msgs, footer = bot.sent[:5], bot.sent[5]
    assert len(bot.sent) == 6
    assert all(button(m).url.startswith("https://x.lk/") for m in job_msgs)  # one job per message
    assert footer.text == "Showing 5 of 8. Tap below for the rest."
    assert button(footer).text == "Thawa Jobs Pennanna (Show More)"

    query = FakeQuery(button(footer).callback_data)
    await show_more(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7)), ctx)
    assert query.markup_removed
    more = bot.sent[6:]
    assert [m.text.split("</b>")[0] for m in more] == ["<b>6. Driver 5", "<b>7. Driver 6", "<b>8. Driver 7"]


async def test_no_footer_when_everything_fits(storage):
    jobs = [Job(url=f"https://x.lk/{i}", title=f"Driver {i}") for i in range(2)]
    bot = FakeBot()
    msg = FakeMessage("driver")
    await search(SimpleNamespace(effective_message=msg, effective_user=SimpleNamespace(id=7)),
                 fake_context(make_service(storage, FakeSource(jobs=jobs)), bot))
    assert msg.status.edits == ["🔎 Found 2 new jobs for <b>driver</b>:"]
    assert len(bot.sent) == 2 and all(button(m).url for m in bot.sent)


# --- category menu ----------------------------------------------------------------

def test_categories_are_valid_buttons():
    ids = [c.id for c in CATEGORIES]
    assert len(ids) == len(set(ids))
    for c in CATEGORIES:
        assert len((CATEGORY_PREFIX + c.id).encode()) <= 64  # Telegram's callback_data limit
        assert c.keyword == normalize_keyword(c.keyword)  # same form as a typed keyword
        assert category_by_id(c.id) is c
    assert category_by_id("nope") is None


def test_category_keyboard_two_per_row():
    rows = category_keyboard().inline_keyboard
    assert all(len(row) <= 2 for row in rows)
    flat = [b for row in rows for b in row]
    assert [b.text for b in flat] == [c.label for c in CATEGORIES]
    assert [b.callback_data for b in flat] == [f"{CATEGORY_PREFIX}{c.id}" for c in CATEGORIES]


async def test_start_and_categories_show_the_menu():
    for handler in (start, categories):
        msg = FakeMessage("/x")
        await handler(SimpleNamespace(effective_message=msg), None)
        assert msg.reply_markups[0].inline_keyboard == category_keyboard().inline_keyboard


async def test_category_button_searches_its_keyword(storage):
    jobs = [Job(url=f"https://x.lk/{i}", title=f"QA Engineer {i}", source="TopJobs.lk") for i in range(2)]
    bot = FakeBot()
    query = FakeQuery(f"{CATEGORY_PREFIX}qa")
    await choose_category(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7)),
                          fake_context(make_service(storage, FakeSource(jobs=jobs)), bot))
    assert query.answers == [((), {})]
    assert not query.markup_removed  # the menu stays for the next pick
    texts = [m.text for m in bot.sent]
    assert texts[0].startswith("🔎 Searching for <b>qa</b>")
    assert "🔎 Found 2 new jobs for <b>qa</b>:" in texts
    assert sum(1 for m in bot.sent if getattr(m, "reply_markup", None) and button(m).url) == 2


async def test_unknown_category_alerts_without_searching(storage):
    bot = FakeBot()
    query = FakeQuery(f"{CATEGORY_PREFIX}gone")
    await choose_category(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=7)),
                          fake_context(make_service(storage, FakeSource()), bot))
    (args, kwargs), = query.answers
    assert "no longer available" in args[0] and kwargs["show_alert"]
    assert bot.sent == []
