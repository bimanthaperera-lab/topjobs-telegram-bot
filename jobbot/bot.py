"""Telegram handlers and application wiring."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from html import escape

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .categories import CATEGORIES, category_by_id
from .formatting import TELEGRAM_MAX_CAPTION, format_job, telegram_length
from .health import start_health_server
from .job_service import PAGE_SIZE, FetchError, JobSearchService
from .images import JobImages
from .models import Job
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
JOB_BUTTON_LABEL = "🔗 View & Apply"
MAX_BUTTON_URL = 2000
CALLBACK_PREFIX = "more:"
CATEGORY_PREFIX = "cat:"
CATEGORY_COLUMNS = 2

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

MSG_WELCOME = (
    "👋 Welcome! I find <b>IT and software jobs</b> in Sri Lanka.\n\n"
    "Tap a category below, or send me a keyword, for example <i>react</i>, <i>java</i> or "
    "<i>business analyst</i>, and I'll show the best matching openings with their job ads."
)
MSG_PICK_CATEGORY = "Pick a category, or send me any keyword:"
MSG_UNKNOWN_CATEGORY = "This category is no longer available. Send /categories to see the current list."
MSG_ASK_KEYWORD = "Please send a keyword, for example <i>software engineer</i>."
MSG_TOO_LONG = f"That keyword is too long. Please keep it under {MAX_KEYWORD_LENGTH} characters."
MSG_BUSY = "⏳ I'm still working on your previous search. Please wait for it to finish."
MSG_SEARCHING = "🔎 Searching for <b>{keyword}</b> jobs..."
MSG_FOUND = "🔎 Found {count} new job{plural} for <b>{keyword}</b>:"
MSG_SHOWING = "Showing {shown} of {total}. Tap below for the rest."
MSG_NO_NEW = (
    "No new jobs matching <b>{keyword}</b> right now. "
    "Either you've seen them all, or none fit. Try again later, or use different words."
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
BOT_COMMANDS = [
    BotCommand("start", "Welcome message and categories"),
    BotCommand("categories", "Browse IT job categories"),
]


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


async def _warn(context: ContextTypes.DEFAULT_TYPE, reply, user_id: int, text: str) -> None:
    """Reply to a blocked request, but at most once per WARN_INTERVAL_SECS per user so
    a flooding user cannot make the bot flood Telegram back."""
    warned: dict[int, float] = context.application.bot_data.setdefault("warned_at", {})
    now = time.monotonic()
    if now - warned.get(user_id, float("-inf")) < WARN_INTERVAL_SECS:
        return
    warned[user_id] = now
    try:
        await reply(text)
    except TelegramError:
        log.warning("Could not send a rate-limit notice", exc_info=True)


def category_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(c.label, callback_data=f"{CATEGORY_PREFIX}{c.id}") for c in CATEGORIES]
    return InlineKeyboardMarkup(
        [buttons[i:i + CATEGORY_COLUMNS] for i in range(0, len(buttons), CATEGORY_COLUMNS)]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            MSG_WELCOME, parse_mode=ParseMode.HTML, reply_markup=category_keyboard()
        )


async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(MSG_PICK_CATEGORY, reply_markup=category_keyboard())


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
    await _run_search(context, message.chat_id, user.id, keyword, message.reply_text)


async def choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A category button: searches the category's title phrases, best kind first. The
    menu stays, so the user can tap another category later."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    category = category_by_id((query.data or "")[len(CATEGORY_PREFIX):])
    if category is None:
        await query.answer(MSG_UNKNOWN_CATEGORY, show_alert=True)
        return
    await query.answer()
    chat_id = query.message.chat.id if query.message else user.id
    await _run_search(context, chat_id, user.id, category.label,
                      functools.partial(context.bot.send_message, chat_id), any_of=category.phrases)


async def _run_search(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, keyword: str,
                      reply: Callable[..., Awaitable], any_of: tuple[str, ...] = ()) -> None:
    """Search and send the results. reply(text, **kwargs) sends a message to the chat.
    any_of: a category's phrases; keyword is then the category's label."""
    # Checks and records below have no await in between, so they are atomic on the event loop.
    active = _active_users(context)
    if user_id in active:
        await _warn(context, reply, user_id, MSG_BUSY)
        return
    limiter = _limiter(context)
    if limiter is not None:
        decision = limiter.check_search(user_id)
        if not decision.allowed:
            await _warn(context, reply, user_id, _limit_message(decision))
            return
        limiter.record_search(user_id)
    active.add(user_id)

    escaped_keyword = escape(keyword)
    status = None
    try:
        status = await reply(
            MSG_SEARCHING.format(keyword=escaped_keyword), parse_mode=ParseMode.HTML
        )
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

        service = _service(context)
        result = await service.search(user_id, keyword, any_of)

        notice = _source_notice(result.failed_sources, result.stale_sources)
        if not result.first_page:
            text = MSG_NO_NEW.format(keyword=escaped_keyword)
            await _edit_or_send(status, chat_id, context, f"{text}\n\n{notice}" if notice else text)
            return

        # The "Searching..." message becomes the header, then one message per job.
        total = len(result.first_page) + result.more_count
        header = MSG_FOUND.format(count=total, plural="" if total == 1 else "s", keyword=escaped_keyword)
        await _edit_or_send(status, chat_id, context, f"{header}\n{notice}" if notice else header)
        await send_jobs(context.bot, chat_id, result.first_page, start_index=1,
                        images=context.application.bot_data.get("images"))
        service.record_sent(user_id, keyword, result.first_page)

        if result.more_session_id:
            await context.bot.send_message(
                chat_id,
                MSG_SHOWING.format(shown=len(result.first_page), total=total),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(SHOW_MORE_LABEL, callback_data=f"{CALLBACK_PREFIX}{result.more_session_id}")]]
                ),
            )
    except RateLimitExceeded as exc:
        await _edit_or_send(status, chat_id, context, _limit_message(exc.decision))
    except FetchError:
        log.warning("Every job site failed for keyword %r", keyword)
        await _edit_or_send(status, chat_id, context, MSG_FAILED)
    except TelegramError:
        log.exception("Telegram error while sending results for %r", keyword)
    except Exception:
        log.exception("Unexpected error while searching %r", keyword)
        await _edit_or_send(status, chat_id, context, MSG_UNEXPECTED)
    finally:
        active.discard(user_id)


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
        await send_jobs(context.bot, chat_id, jobs, start_index=PAGE_SIZE + 1,
                        images=context.application.bot_data.get("images"))
        service.record_more_sent(user.id, jobs)
    except TelegramError:
        log.exception("Telegram error while sending Show More results")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)


def job_keyboard(job: Job) -> InlineKeyboardMarkup | None:
    """A 'View & Apply' button that opens the job page, or None if the URL can't be a button."""
    if not job.url.lower().startswith(("https://", "http://")) or len(job.url) > MAX_BUTTON_URL:
        return None
    label = f"{JOB_BUTTON_LABEL} on {job.source}" if job.source else JOB_BUTTON_LABEL
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, url=job.url)]])


async def send_job(bot, chat_id: int, job: Job, index: int, images: JobImages | None = None,
                   tag: str | None = None) -> None:
    """One job per message: the ad image with the details as its caption when there is one,
    otherwise a text message; the link is an inline button under it either way.
    tag: an optional first line, e.g. "🆕 New Match"."""
    keyboard = job_keyboard(job)
    photo = await images.photo_for(job) if images is not None else None
    if photo is not None:
        caption = format_job(job, index, include_link=keyboard is None, tag=tag)
        if telegram_length(caption) <= TELEGRAM_MAX_CAPTION:
            try:
                sent = await bot.send_photo(chat_id, photo, caption=caption,
                                            parse_mode=ParseMode.HTML, reply_markup=keyboard)
                if isinstance(photo, bytes) and getattr(sent, "photo", None):
                    images.remember(job.url, sent.photo[-1].file_id)  # reuse for every later send
                return
            except BadRequest as exc:
                log.warning("Photo for %s rejected (%s); sending text instead", job.url, exc)
    if keyboard is not None:
        try:
            await bot.send_message(chat_id, format_job(job, index, include_link=False, tag=tag),
                                   parse_mode=ParseMode.HTML, link_preview_options=_NO_PREVIEW,
                                   reply_markup=keyboard)
            return
        except BadRequest as exc:
            # Telegram rejects some URLs as buttons; fall back to a text link.
            log.warning("Job button rejected (%s); sending a text link instead", exc)
    await bot.send_message(chat_id, format_job(job, index, include_link=True, tag=tag),
                           parse_mode=ParseMode.HTML, link_preview_options=_NO_PREVIEW)


async def send_jobs(bot, chat_id: int, jobs: list[Job], start_index: int, images: JobImages | None = None) -> None:
    for offset, job in enumerate(jobs):
        await send_job(bot, chat_id, job, start_index + offset, images)


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
    images: JobImages | None = None,
) -> Application:
    async def post_init(app: Application) -> None:
        try:
            await app.bot.set_my_commands(BOT_COMMANDS)  # the "/" menu in Telegram
        except TelegramError:
            log.warning("Could not register the bot's command menu", exc_info=True)
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
        .rate_limiter(AIORateLimiter(max_retries=3))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.bot_data["service"] = service
    app.bot_data["limiter"] = limiter
    app.bot_data["images"] = images
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CallbackQueryHandler(show_more, pattern=f"^{CALLBACK_PREFIX}"))
    app.add_handler(CallbackQueryHandler(choose_category, pattern=f"^{CATEGORY_PREFIX}"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search))
    app.add_error_handler(on_error)
    return app
