"""Telegram handlers and application wiring."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .formatting import format_jobs
from .health import start_health_server
from .job_service import PAGE_SIZE, FetchError, JobSearchService
from .normalize import MAX_KEYWORD_LENGTH, normalize_keyword
from .rate_limit import (
    REASON_COOLDOWN,
    REASON_GLOBAL_FETCHES,
    REASON_HOURLY,
    REASON_USER_FETCHES,
    Decision,
    RateLimiter,
    RateLimitExceeded,
    format_wait,
)

log = logging.getLogger(__name__)

SHOW_MORE_LABEL = "Thawa Jobs Pennanna (Show More)"
CALLBACK_PREFIX = "more:"

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

MSG_WELCOME = (
    "👋 Welcome to the Job Search Bot!\n\n"
    "Send me a job keyword (for example <i>accountant</i> or <i>software engineer</i>) "
    "and I'll find the latest openings for you."
)
MSG_ASK_KEYWORD = "Please send a job keyword, for example <i>accountant</i>."
MSG_TOO_LONG = f"That keyword is too long. Please keep it under {MAX_KEYWORD_LENGTH} characters."
MSG_BUSY = "⏳ I'm still working on your previous search. Please wait for it to finish."
MSG_SEARCHING = "🔎 Searching for <b>{keyword}</b> jobs..."
MSG_NO_NEW = (
    "No new jobs found for <b>{keyword}</b> right now. "
    "You've already seen the latest results. Please try again later or search for another keyword."
)
MSG_FAILED = "⚠️ Sorry, the job sites couldn't be reached right now. Please try again in a few minutes."
MSG_SOURCE_DOWN = "<i>⚠️ {sites} couldn't be reached, so these results may be incomplete.</i>"
MSG_SOURCE_STALE = "<i>🕒 {sites} couldn't be reached right now; showing its earlier results, which may be outdated.</i>"
MSG_UNEXPECTED = "⚠️ Something went wrong. Please try again."
MSG_EXPIRED = "This button has expired or was already used. Send a keyword to search again."
MSG_NOTHING_MORE = "No more new jobs for this search. Send a keyword to search again."
MSG_LIMIT_DEFAULT = "🚦 Too many requests. Please try again in {wait}."
_LIMIT_MESSAGES = {
    REASON_COOLDOWN: "⏳ Please wait {wait} before searching again.",
    REASON_HOURLY: "🚦 You've reached the search limit for this hour. Please try again in {wait}.",
    REASON_USER_FETCHES: (
        "🚦 You've reached today's limit for new searches. Please try again in {wait}. "
        "Keywords searched recently may still work."
    ),
    REASON_GLOBAL_FETCHES: "🚦 The bot has reached its daily search limit. Please try again in {wait}.",
}
WARN_INTERVAL_SECS = 5


def _service(context: ContextTypes.DEFAULT_TYPE) -> JobSearchService:
    return context.application.bot_data["service"]


def _active_users(context: ContextTypes.DEFAULT_TYPE) -> set[int]:
    return context.application.bot_data.setdefault("active_users", set())


def _limiter(context: ContextTypes.DEFAULT_TYPE) -> RateLimiter | None:
    return context.application.bot_data.get("limiter")


def _source_notice(failed_sources: list[str], stale_sources: list[str] = ()) -> str | None:
    lines = []
    if failed_sources:
        lines.append(MSG_SOURCE_DOWN.format(sites=escape(", ".join(failed_sources))))
    if stale_sources:
        lines.append(MSG_SOURCE_STALE.format(sites=escape(", ".join(stale_sources))))
    return "\n".join(lines) or None


def _limit_message(decision: Decision) -> str:
    template = _LIMIT_MESSAGES.get(decision.reason or "", MSG_LIMIT_DEFAULT)
    return template.format(wait=format_wait(decision.retry_after_secs))


async def _warn(context: ContextTypes.DEFAULT_TYPE, message, user_id: int, text: str) -> None:
    """Reply to a blocked request, but at most once per WARN_INTERVAL_SECS per user so
    a flooding user cannot make the bot flood Telegram back."""
    warned: dict[int, float] = context.application.bot_data.setdefault("warned_at", {})
    now = time.monotonic()
    if now - warned.get(user_id, float("-inf")) < WARN_INTERVAL_SECS:
        return
    warned[user_id] = now
    try:
        await message.reply_text(text)
    except TelegramError:
        log.warning("Could not send a rate-limit notice", exc_info=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(MSG_WELCOME, parse_mode=ParseMode.HTML)


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    keyword = normalize_keyword(message.text)
    if not keyword:
        await message.reply_text(MSG_ASK_KEYWORD, parse_mode=ParseMode.HTML)
        return
    if len(keyword) > MAX_KEYWORD_LENGTH:
        await message.reply_text(MSG_TOO_LONG)
        return

    # Checks and records below have no await in between, so they are atomic on the event loop.
    active = _active_users(context)
    if user.id in active:
        await _warn(context, message, user.id, MSG_BUSY)
        return
    limiter = _limiter(context)
    if limiter is not None:
        decision = limiter.check_search(user.id)
        if not decision.allowed:
            await _warn(context, message, user.id, _limit_message(decision))
            return
        limiter.record_search(user.id)
    active.add(user.id)

    escaped_keyword = escape(keyword)
    status = None
    try:
        status = await message.reply_text(
            MSG_SEARCHING.format(keyword=escaped_keyword), parse_mode=ParseMode.HTML
        )
        await context.bot.send_chat_action(message.chat_id, ChatAction.TYPING)

        service = _service(context)
        result = await service.search(user.id, keyword)

        notice = _source_notice(result.failed_sources, result.stale_sources)
        if not result.first_page:
            text = MSG_NO_NEW.format(keyword=escaped_keyword)
            await _edit_or_send(status, message.chat_id, context, f"{text}\n\n{notice}" if notice else text)
            return

        header = f"Found jobs for <b>{escaped_keyword}</b>:"
        if notice:
            header = f"{header}\n{notice}"
        markup = (
            InlineKeyboardMarkup(
                [[InlineKeyboardButton(SHOW_MORE_LABEL, callback_data=f"{CALLBACK_PREFIX}{result.more_session_id}")]]
            )
            if result.more_session_id
            else None
        )
        await _send_pages(context, message.chat_id, format_jobs(result.first_page, header=header), markup)
        service.record_sent(user.id, keyword, result.first_page)
        await _delete_quietly(status)
    except RateLimitExceeded as exc:
        await _edit_or_send(status, message.chat_id, context, _limit_message(exc.decision))
    except FetchError:
        log.warning("Every job site failed for keyword %r", keyword)
        await _edit_or_send(status, message.chat_id, context, MSG_FAILED)
    except TelegramError:
        log.exception("Telegram error while sending results for %r", keyword)
    except Exception:
        log.exception("Unexpected error while searching %r", keyword)
        await _edit_or_send(status, message.chat_id, context, MSG_UNEXPECTED)
    finally:
        active.discard(user.id)


async def show_more(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    session_id = (query.data or "")[len(CALLBACK_PREFIX):]

    service = _service(context)
    # Reads SQLite only; never contacts a job site.
    jobs = service.show_more(user.id, session_id)
    if jobs is None:
        await query.answer(MSG_EXPIRED, show_alert=True)
        await _remove_button(query)
        return

    await query.answer()
    await _remove_button(query)
    chat_id = query.message.chat.id if query.message else user.id
    if not jobs:
        await context.bot.send_message(chat_id, MSG_NOTHING_MORE)
        return
    try:
        # Numbering continues after the first page.
        await _send_pages(context, chat_id, format_jobs(jobs, start_index=PAGE_SIZE + 1), None)
        service.record_more_sent(user.id, jobs)
    except TelegramError:
        log.exception("Telegram error while sending Show More results")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)


async def _send_pages(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, pages: list[str], markup: InlineKeyboardMarkup | None
) -> None:
    for i, page in enumerate(pages):
        is_last = i == len(pages) - 1
        await context.bot.send_message(
            chat_id,
            page,
            parse_mode=ParseMode.HTML,
            link_preview_options=_NO_PREVIEW,
            reply_markup=markup if is_last else None,
        )


async def _edit_or_send(status, chat_id: int, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    try:
        if status is not None:
            await status.edit_text(text, parse_mode=ParseMode.HTML)
            return
    except TelegramError:
        pass
    try:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramError:
        log.exception("Could not send a message to chat %s", chat_id)


async def _delete_quietly(status) -> None:
    if status is None:
        return
    try:
        await status.delete()
    except TelegramError:
        pass


async def _remove_button(query) -> None:
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass  # already removed (e.g. second click) or message too old to edit
    except TelegramError:
        log.warning("Could not remove the Show More button", exc_info=True)


def build_application(
    token: str,
    service: JobSearchService,
    limiter: RateLimiter | None = None,
    health_port: int | None = None,
    shutdown_hooks: list[Callable[[], Awaitable[None]]] | None = None,
) -> Application:
    async def post_init(app: Application) -> None:
        if health_port is not None:
            app.bot_data["health_server"] = await start_health_server(health_port)

    async def post_shutdown(app: Application) -> None:
        server = app.bot_data.get("health_server")
        if server is not None:
            server.close()
            await server.wait_closed()
        for hook in shutdown_hooks or []:
            await hook()

    # concurrent_updates: a slow site for one user must not stall everyone else.
    app = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["service"] = service
    app.bot_data["limiter"] = limiter
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(show_more, pattern=f"^{CALLBACK_PREFIX}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
    app.add_error_handler(on_error)
    return app
