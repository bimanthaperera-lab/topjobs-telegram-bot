"""Job sites the bot searches directly (no third-party scraping service)."""

from __future__ import annotations

from typing import Protocol

from ..models import Job


class SourceError(RuntimeError):
    """A site could not be searched (network error, bad status, unexpected page)."""


class SiteBlockedError(SourceError):
    """The site answered 402/403/429: it is refusing or rate-limiting the bot."""

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
    #   refreshed_in_background: bool - (keyword_independent too) too slow to fetch during a
    #                                search: the service keeps its list fresh in the background.
    #   async ad_image_url(job_url) -> str | None  - the URL of the job's ad image.
    #   async download_image(image_url) -> bytes | None  - that image, if Telegram can't fetch it.
