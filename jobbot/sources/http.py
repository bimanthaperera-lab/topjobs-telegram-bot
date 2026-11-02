"""HTTP client that is gentle with each site and backs off when a site pushes back.

- At most one request per `min_interval_secs` per host, across all users.
- Timeouts, connection errors and 5xx answers are retried with exponential backoff.
- 403/429 answers are not retried: the host is paused (honouring Retry-After) and
  every request to it fails fast until the pause ends.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from . import SiteBlockedError, SitePausedError, SourceError

DEFAULT_USER_AGENT = "TopJobsTelegramBot/1.0 (+https://t.me/TopJobsSearch_Bot)"
BLOCK_STATUSES = frozenset({403, 429})
MAX_PAUSE_SECS = 24 * 3600


def retry_after_secs(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


class PoliteHttpClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        min_interval_secs: float,
        *,
        retries: int = 2,
        retry_backoff_secs: float = 2.0,
        block_pause_secs: float = 30 * 60,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._client = client
        self._min_interval = min_interval_secs
        self._retries = retries
        self._retry_backoff = retry_backoff_secs
        self._block_pause = block_pause_secs
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._paused_until: dict[str, float] = {}

    def paused_for(self, host: str) -> float:
        """Seconds left on a host's pause (0 if not paused)."""
        return max(0.0, self._paused_until.get(host, 0.0) - self._clock())

    async def _send_once(self, host: str, url: str, **kwargs) -> httpx.Response:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_request.get(host)
            if last is not None:
                wait = last + self._min_interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            try:
                return await self._client.get(url, **kwargs)
            finally:
                self._last_request[host] = self._clock()

    async def get(self, url: str, **kwargs) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        last_error = ""
        for attempt in range(self._retries + 1):
            remaining = self.paused_for(host)
            if remaining > 0:
                raise SitePausedError(host, remaining)

            try:
                response = await self._send_once(host, url, **kwargs)
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            else:
                status = response.status_code
                if status == 200:
                    return response
                if status in BLOCK_STATUSES:
                    asked = retry_after_secs(response.headers.get("Retry-After"))
                    pause = min(MAX_PAUSE_SECS, max(self._block_pause, asked or 0))
                    self._paused_until[host] = self._clock() + pause
                    raise SiteBlockedError(host, status, pause)
                if status < 500:
                    raise SourceError(f"{host}: HTTP {status}")  # 4xx won't fix itself
                last_error = f"HTTP {status}"

            if attempt < self._retries:
                await self._sleep(self._retry_backoff * (2**attempt))
        raise SourceError(f"{host}: {last_error} after {self._retries + 1} attempts")

    async def aclose(self) -> None:
        await self._client.aclose()


def build_http_client(timeout_secs: float, user_agent: str = DEFAULT_USER_AGENT) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_secs),
        headers={"User-Agent": user_agent, "Accept-Language": "en"},
        follow_redirects=True,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=4),
    )
