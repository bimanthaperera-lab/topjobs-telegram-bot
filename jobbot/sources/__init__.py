"""Job sites the bot searches directly (no third-party scraping service)."""

from __future__ import annotations

from typing import Protocol

from ..models import Job


class SourceError(RuntimeError):
    """A site could not be searched (network error, bad status, unexpected page)."""


class SiteBlockedError(SourceError):
    """The site answered 403/429: it is refusing or rate-limiting the bot."""

    def __init__(self, host: str, status: int, pause_secs: float) -> None:
        super().__init__(f"{host}: HTTP {status}; pausing requests for {round(pause_secs / 60)} min")
        self.status = status
        self.pause_secs = pause_secs


class SitePausedError(SourceError):
    """Requests to the site are on hold after it blocked or rate-limited the bot."""

    def __init__(self, host: str, remaining_secs: float) -> None:
        super().__init__(f"{host}: paused for another {max(1, round(remaining_secs / 60))} min")
        self.remaining_secs = remaining_secs


class JobSource(Protocol):
    name: str
    # True when fetch() returns jobs newest first, which allows stopping at the
    # last job already sent to a user.
    sorted_newest_first: bool

    async def fetch(self, keyword: str, limit: int) -> list[Job]: ...

    # Optional extras (read with getattr):
    #   keyword_independent: bool  - fetch() returns the same list for every keyword,
    #                                so one cached fetch serves all keywords.
    #   async fetch_image(job_url) -> bytes | None  - the job's ad image.
