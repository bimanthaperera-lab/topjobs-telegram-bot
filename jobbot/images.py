"""Job ad images to send with each job.

TopJobs publishes the real job description as an image on the job page. The
first time a job is sent, its image is downloaded and uploaded to Telegram;
Telegram's file_id is then stored, so every later send (any user) reuses it
without touching the site again.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from .models import Job
from .sources import JobSource, SourceError
from .storage import Storage

log = logging.getLogger(__name__)

# A job page without an image is re-checked after this long (the ad may be edited).
NO_IMAGE_RECHECK_SECS = 24 * 3600


class JobImages:
    def __init__(self, storage: Storage, sources: Sequence[JobSource], *, enabled: bool = True) -> None:
        self._storage = storage
        self._sources = {s.name: s for s in sources}
        self._enabled = enabled

    async def photo_for(self, job: Job) -> str | bytes | None:
        """A Telegram file_id (already uploaded), image bytes (upload them), or None."""
        if not self._enabled:
            return None
        known = self._storage.get_job_photo(job.url)
        if known is not None:
            file_id, checked_at = known
            if file_id:
                return file_id
            if self._storage.now() - checked_at < NO_IMAGE_RECHECK_SECS:
                return None
        fetch_image = getattr(self._sources.get(job.source or ""), "fetch_image", None)
        if fetch_image is None:
            return None
        try:
            data = await fetch_image(job.url)
        except SourceError as exc:
            log.warning("Could not get the ad image for %s: %s", job.url, exc)
            return None  # not recorded: try again next time
        if data is None:
            self._storage.save_job_photo(job.url, None)
        return data

    def remember(self, job_url: str, file_id: str) -> None:
        self._storage.save_job_photo(job_url, file_id)
