"""Require users to join a Telegram channel before they can use the bot.

Telegram only answers "is this user in the channel?" (getChatMember) for channels where
the bot is an admin. If the check itself fails (bot not an admin, wrong channel), the
gate lets users through and logs a warning: a setup mistake must not lock everyone out.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Container

from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

log = logging.getLogger(__name__)

MEMBER_CACHE_SECS = 600  # a "yes" is reused this long; a "no" is always rechecked
FAILURE_LOG_INTERVAL_SECS = 600
_JOINED = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER}


def channel_link(channel: str) -> str | None:
    """The public t.me link for "@name"; None for a numeric (private) channel id."""
    return f"https://t.me/{channel[1:]}" if channel.startswith("@") else None


class ChannelGate:
    def __init__(self, channel: str, link: str, exempt_user_ids: Container[int] = frozenset(),
                 cache_secs: float = MEMBER_CACHE_SECS, clock: Callable[[], float] = time.monotonic) -> None:
        self.channel = channel
        self.link = link
        self._chat_id: str | int = int(channel) if channel.lstrip("-").isdigit() else channel
        self._exempt = exempt_user_ids
        self._cache_secs = cache_secs
        self._clock = clock
        self._joined_at: dict[int, float] = {}
        self._last_failure_log = float("-inf")

    async def is_member(self, bot, user_id: int, *, fresh: bool = False) -> bool:
        """fresh: ignore a cached "yes" (e.g. right after the user says they joined)."""
        if user_id in self._exempt:
            return True
        now = self._clock()
        if not fresh and now - self._joined_at.get(user_id, float("-inf")) < self._cache_secs:
            return True
        try:
            member = await bot.get_chat_member(self._chat_id, user_id)
        except TelegramError as exc:
            if now - self._last_failure_log >= FAILURE_LOG_INTERVAL_SECS:
                self._last_failure_log = now
                log.warning("Could not check membership of %s (%s); letting users in. "
                            "Is the bot an admin of the channel?", self.channel, exc)
            return True
        joined = member.status in _JOINED or (
            member.status == ChatMemberStatus.RESTRICTED and getattr(member, "is_member", False)
        )
        if joined:
            self._joined_at[user_id] = now
        else:
            self._joined_at.pop(user_id, None)
        return joined
