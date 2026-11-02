"""Required channel: membership check, caching, fail-open, and the gated handlers."""

from types import SimpleNamespace

import pytest
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest

from jobbot.bot import JOINED_CALLBACK, check_joined, members_only
from jobbot.channel_gate import ChannelGate, channel_link
from jobbot.config import ConfigError, load_settings

from .test_bot import FakeBot, FakeQuery

LINK = "https://t.me/lk_jobs"


class MemberBot(FakeBot):
    """FakeBot that answers getChatMember from a {user_id: status} map."""

    def __init__(self, statuses=None, fail=False):
        super().__init__()
        self.statuses = statuses or {}
        self.fail = fail
        self.checks = 0

    async def get_chat_member(self, chat_id, user_id):
        self.checks += 1
        if self.fail:
            raise BadRequest("Member list is inaccessible")
        status = self.statuses.get(user_id, ChatMemberStatus.LEFT)
        return SimpleNamespace(status=status, is_member=False)


def gate(**kwargs):
    return ChannelGate("@lk_jobs", LINK, **kwargs)


@pytest.mark.parametrize("status, joined", [
    (ChatMemberStatus.MEMBER, True),
    (ChatMemberStatus.ADMINISTRATOR, True),
    (ChatMemberStatus.OWNER, True),
    (ChatMemberStatus.LEFT, False),
    (ChatMemberStatus.BANNED, False),
])
async def test_member_statuses(status, joined):
    assert await gate().is_member(MemberBot({7: status}), 7) is joined


async def test_restricted_counts_only_if_still_a_member():
    bot = MemberBot()
    bot.get_chat_member = lambda chat_id, user_id: _async(
        SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=user_id == 1))
    assert await gate().is_member(bot, 1)
    assert not await gate().is_member(bot, 2)


async def _async(value):
    return value


async def test_yes_is_cached_but_no_is_always_rechecked():
    now = [0.0]
    g = gate(clock=lambda: now[0], cache_secs=600)
    bot = MemberBot({7: ChatMemberStatus.MEMBER})
    assert await g.is_member(bot, 7) and await g.is_member(bot, 7)
    assert bot.checks == 1
    assert await g.is_member(bot, 7, fresh=True)
    assert bot.checks == 2
    now[0] = 601
    await g.is_member(bot, 7)
    assert bot.checks == 3

    assert not await g.is_member(bot, 8) and not await g.is_member(bot, 8)
    assert bot.checks == 5


async def test_admins_skip_the_check():
    bot = MemberBot()
    assert await gate(exempt_user_ids={7}).is_member(bot, 7)
    assert bot.checks == 0


async def test_check_failure_lets_users_in():
    assert await gate().is_member(MemberBot(fail=True), 7)


def test_numeric_channel_id_is_used_as_a_number():
    assert ChannelGate("-1001234567890", LINK)._chat_id == -1001234567890
    assert channel_link("@lk_jobs") == LINK and channel_link("-100123") is None


# --- gated handlers -------------------------------------------------------------------

def gated_context(bot, g):
    app = SimpleNamespace(bot_data={"gate": g, "limiter": None})
    return SimpleNamespace(application=app, bot=bot)


def update_for(user_id, query=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=42),
                           callback_query=query)


async def test_non_member_gets_join_prompt_instead_of_the_handler():
    ran = []

    async def handler(update, context):
        ran.append(update.effective_user.id)

    bot = MemberBot({1: ChatMemberStatus.MEMBER})
    ctx = gated_context(bot, gate())
    await members_only(handler)(update_for(1), ctx)
    await members_only(handler)(update_for(2), ctx)
    assert ran == [1]
    (prompt,) = bot.sent
    assert "join our channel" in prompt.text
    join, joined = prompt.reply_markup.inline_keyboard
    assert join[0].url == LINK and joined[0].callback_data == JOINED_CALLBACK


async def test_gated_button_tap_is_answered():
    async def handler(update, context):
        raise AssertionError("must not run")

    query = FakeQuery("cat:qa")
    await members_only(handler)(update_for(2, query), gated_context(MemberBot(), gate()))
    assert query.answers  # the button's spinner stops


async def test_no_gate_means_open():
    ran = []

    async def handler(update, context):
        ran.append(True)

    await members_only(handler)(update_for(2), gated_context(MemberBot(), None))
    assert ran == [True]


async def test_ive_joined_button():
    bot = MemberBot()
    ctx = gated_context(bot, gate())
    query = FakeQuery(JOINED_CALLBACK)
    await check_joined(update_for(7, query), ctx)
    (args, kwargs), = query.answers
    assert "haven't joined" in args[0] and kwargs["show_alert"]
    assert bot.sent == []

    bot.statuses[7] = ChatMemberStatus.MEMBER
    query = FakeQuery(JOINED_CALLBACK)
    await check_joined(update_for(7, query), ctx)
    assert query.markup_removed
    (welcome,) = bot.sent
    assert welcome.text.startswith("👋 Welcome") and welcome.reply_markup.inline_keyboard


# --- config ---------------------------------------------------------------------------

FAKE_TOKEN = "123456:" + "A" * 35


def settings_with(monkeypatch, **values):
    for key in ("REQUIRED_CHANNEL", "REQUIRED_CHANNEL_LINK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return load_settings(env_file=None)


def test_required_channel_is_optional(monkeypatch):
    assert settings_with(monkeypatch).required_channel is None


@pytest.mark.parametrize("raw", ["@lk_jobs", "https://t.me/lk_jobs", "t.me/lk_jobs/"])
def test_public_channel_gets_its_link(monkeypatch, raw):
    s = settings_with(monkeypatch, REQUIRED_CHANNEL=raw)
    assert (s.required_channel, s.required_channel_link) == ("@lk_jobs", LINK)


def test_private_channel_needs_an_invite_link(monkeypatch):
    with pytest.raises(ConfigError, match="REQUIRED_CHANNEL_LINK"):
        settings_with(monkeypatch, REQUIRED_CHANNEL="-1001234567890")
    s = settings_with(monkeypatch, REQUIRED_CHANNEL="-1001234567890", REQUIRED_CHANNEL_LINK="https://t.me/+abc")
    assert s.required_channel_link == "https://t.me/+abc"


@pytest.mark.parametrize("raw", ["lk_jobs", "@x", "12345"])
def test_bad_channel_is_rejected(monkeypatch, raw):
    with pytest.raises(ConfigError, match="REQUIRED_CHANNEL"):
        settings_with(monkeypatch, REQUIRED_CHANNEL=raw)
