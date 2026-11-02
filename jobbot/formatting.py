"""HTML message formatting for Telegram."""

from __future__ import annotations

from html import escape

from .models import Job

TELEGRAM_MAX_MESSAGE = 4096
TELEGRAM_MAX_CAPTION = 1024  # photo captions are much shorter than messages
DESCRIPTION_MAX = 200
_TITLE_MAX = 300
_FIELD_MAX = 150
_URL_MAX = 2000
_SEPARATOR = "\n\n"


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def telegram_length(text: str) -> int:
    """Telegram counts UTF-16 code units; measuring the raw HTML over-counts, which is safe."""
    return len(text.encode("utf-16-le")) // 2


def format_job(job: Job, index: int | None = None, include_link: bool = True, tag: str | None = None) -> str:
    """One job as HTML. Scraped text is escaped; missing fields are left out.

    include_link=False when the link is shown as an inline button instead.
    tag: an optional first line, e.g. "🆕 New Match".
    """
    prefix = f"{index}. " if index is not None else ""
    head = [f"<b>{escape(tag)}</b>"] if tag else []
    lines = head + [f"<b>{prefix}{escape(truncate(job.title, _TITLE_MAX))}</b>"]
    if job.company:
        lines.append(f"🏢 {escape(truncate(job.company, _FIELD_MAX))}")
    if job.location:
        lines.append(f"📍 {escape(truncate(job.location, _FIELD_MAX))}")
    if job.posted:
        lines.append(f"📅 Posted: {escape(truncate(job.posted, _FIELD_MAX))}")
    if job.closing:
        lines.append(f"⏳ Closes: {escape(truncate(job.closing, _FIELD_MAX))}")
    if job.description:
        lines.append(f"📝 {escape(truncate(job.description, DESCRIPTION_MAX))}")
    href = escape(job.url, quote=True)
    label = f"View on {escape(job.source)}" if job.source else "View job"
    link = f'🔗 <a href="{href}">{label}</a>' if include_link and len(href) <= _URL_MAX else None
    if link:
        lines.append(link)
    block = "\n".join(lines)
    if telegram_length(block) > TELEGRAM_MAX_MESSAGE:
        # Pathological input (e.g. text that escapes to many entities): fall back to title + link.
        block = "\n".join(head + [x for x in (f"<b>{prefix}{escape(truncate(job.title, 100))}</b>", link) if x])
    return block


def pack_messages(blocks: list[str], limit: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    """Join blocks into as few messages as possible, each within Telegram's limit."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}{_SEPARATOR}{block}" if current else block
        if telegram_length(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages


def format_jobs(jobs: list[Job], start_index: int = 1, header: str | None = None) -> list[str]:
    blocks = [format_job(job, start_index + i) for i, job in enumerate(jobs)]
    if header:
        blocks.insert(0, header)
    return pack_messages(blocks)
