"""Job ad images to send with each job.

TopJobs publishes the real job description as an image on the job page. The
first time a job is sent, its image URL is found on the job page and passed to
Telegram, which downloads the image itself (much faster than downloading it
here and uploading it again). Only if Telegram can't fetch it is the image
downloaded here and uploaded. Telegram's file_id is then stored, so every later
send (any user) reuses it without touching the site again.

A job without an ad image is sent with its company logo instead, when the site
gives one (XpressJobs): its URL is passed to Telegram the same way. When
Telegram can't get an ad image (an XpressJobs job without one), that is
remembered like a page without one, and the logo is sent.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from .models import Job
from .sources import JobSource, SourceError
from .storage import Storage

log = logging.getLogger(__name__)

# A job page without an image is re-checked after this long (the ad may be edited).
NO_IMAGE_RECHECK_SECS = 24 * 3600


@dataclass(frozen=True)
class Photo:
    file: str | bytes  # a Telegram file_id, an image URL, or image bytes
    is_ad_url: bool = False  # the job's ad image URL: store the file_id, and download it if Telegram can't
    logo: str | None = None  # the company logo URL, if the ad image can't be sent


class JobImages:
    def __init__(self, storage: Storage, sources: Sequence[JobSource], *, enabled: bool = True) -> None:
        self._storage = storage
        self._sources = {s.name: s for s in sources}
        self._enabled = enabled

    async def photo_for(self, job: Job) -> Photo | None:
        """The job's ad image (a stored file_id or its URL), else its company logo, else None."""
        if not self._enabled:
            return None
        ad_image = await self._ad_image(job)
        if ad_image is not None:
            return Photo(ad_image.file, ad_image.is_ad_url, logo=job.logo_url)
        return Photo(job.logo_url) if job.logo_url else None

    async def _ad_image(self, job: Job) -> Photo | None:
        known = self._storage.get_job_photo(job.url)
        if known is not None:
            file_id, checked_at = known
            if file_id:
                return Photo(file_id)
            if self._storage.now() - checked_at < NO_IMAGE_RECHECK_SECS:
                return None
        ad_image_url = getattr(self._sources.get(job.source or ""), "ad_image_url", None)
        if ad_image_url is None:
            return None
        try:
            url = await ad_image_url(job.url)
        except SourceError as exc:
            log.warning("Could not get the ad image for %s: %s", job.url, exc)
            return None  # not recorded: try again next time
        if url is None:
            self._storage.save_job_photo(job.url, None)
            return None
        return Photo(url, is_ad_url=True)

    async def download(self, job: Job, image_url: str) -> bytes | None:
        """Download the ad image here, for when Telegram couldn't fetch its URL."""
        download_image = getattr(self._sources.get(job.source or ""), "download_image", None)
        if download_image is None:
            return None
        try:
            return await download_image(image_url)
        except SourceError as exc:
            log.warning("Could not download the ad image for %s: %s", job.url, exc)
            return None

    def remember(self, job_url: str, file_id: str) -> None:
        self._storage.save_job_photo(job_url, file_id)

    def no_ad_image(self, job_url: str) -> None:
        """Telegram couldn't use the ad image URL: don't try it again until the recheck."""
        self._storage.save_job_photo(job_url, None)
