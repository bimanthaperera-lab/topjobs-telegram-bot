"""Bot wiring and message helpers (no Telegram network calls)."""

from jobbot.bot import (
    CALLBACK_PREFIX,
    SHOW_MORE_LABEL,
    _source_notice,
    _limit_message,
    build_application,
)
from jobbot.rate_limit import REASON_COOLDOWN, REASON_USER_FETCHES, Decision

from .conftest import FakeSource, make_service


def test_application_builds_with_handlers(storage):
    app = build_application("123456:" + "A" * 35, make_service(storage, FakeSource()))
    names = sorted(type(h).__name__ for h in app.handlers[0])
    assert names == ["CallbackQueryHandler", "CommandHandler", "MessageHandler"]


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
