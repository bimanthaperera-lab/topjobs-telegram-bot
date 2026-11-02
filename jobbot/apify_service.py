"""Runs the Apify Actor with cost controls and maps its dataset to Jobs.

Uses ApifyClientAsync, so Actor runs never block the bot's event loop.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from apify_client import ApifyClientAsync
from apify_client.errors import ApifyApiError

from . import actor_mapping
from .models import Job

log = logging.getLogger(__name__)

# How long to keep waiting for the run after its own timeout should have fired.
_WAIT_GRACE_SECS = 30
_MEMORY_HINTS = ("memory", "out of memory", "oom", "heap")


class ActorRunError(RuntimeError):
    """The Actor run failed and produced no usable results."""


def pick_memory(configured_mb: int, actor_min_mb: int | None) -> int:
    """Use the configured memory unless the Actor declares a higher minimum."""
    return max(configured_mb, actor_min_mb or 0)


def is_low_memory_failure(status: str | None, status_message: str | None, exit_code: int | None) -> bool:
    if status != "FAILED":
        return False
    if exit_code == 137:  # killed by the kernel OOM killer
        return True
    message = (status_message or "").lower()
    return any(hint in message for hint in _MEMORY_HINTS)


class ApifyJobFetcher:
    def __init__(
        self,
        client: ApifyClientAsync,
        actor_id: str,
        *,
        memory_mb: int,
        fallback_memory_mb: int,
        timeout_secs: int,
        fetch_full_details: bool,
    ) -> None:
        self._client = client
        self._actor_id = actor_id
        self._memory_mb = memory_mb
        self._fallback_memory_mb = fallback_memory_mb
        self._timeout_secs = timeout_secs
        self._fetch_full_details = fetch_full_details
        self._actor_min_mb: int | None = None
        self._actor_min_loaded = False
        if not actor_mapping.ACTOR_SORTS_NEWEST:
            log.warning(
                "Actor %s cannot sort results newest first; early stopping at the saved URL is "
                "disabled and duplicates are filtered by sent-URL history only.",
                actor_id,
            )

    async def _actor_min_memory(self) -> int | None:
        """Read minMemoryMbytes from the Actor's latest build (cached; best effort)."""
        if self._actor_min_loaded:
            return self._actor_min_mb
        try:
            actor = await self._client.actor(self._actor_id).get()
            tagged = (actor.tagged_builds or {}) if actor else {}
            latest = tagged.get("latest")
            if latest and latest.build_id:
                build = await self._client.build(latest.build_id).get()
                definition = build.actor_definition if build else None
                self._actor_min_mb = definition.min_memory_mbytes if definition else None
        except Exception as exc:  # noqa: BLE001 - metadata is optional
            log.warning("Could not read Actor minimum memory (%s); using configured value.", type(exc).__name__)
        self._actor_min_loaded = True
        return self._actor_min_mb

    async def _run(self, keyword: str, limit: int, memory_mb: int) -> Any:
        return await self._client.actor(self._actor_id).call(
            run_input=actor_mapping.build_run_input(keyword, limit, self._fetch_full_details),
            max_items=limit,
            memory_mbytes=memory_mb,
            run_timeout=timedelta(seconds=self._timeout_secs),
            wait_duration=timedelta(seconds=self._timeout_secs + _WAIT_GRACE_SECS),
            logger=None,  # don't stream the Actor's log into ours
        )

    async def fetch(self, keyword: str, limit: int) -> list[Job]:
        memory_mb = pick_memory(self._memory_mb, await self._actor_min_memory())
        run = None
        for attempt in (1, 2):
            try:
                run = await self._run(keyword, limit, memory_mb)
            except ApifyApiError as exc:
                low_memory = any(h in (exc.message or "").lower() for h in _MEMORY_HINTS)
                log.warning(
                    "Actor start failed (attempt %d, memory=%dMB): %s", attempt, memory_mb, exc.message
                )
                if attempt == 1 and low_memory and self._fallback_memory_mb > memory_mb:
                    memory_mb = self._fallback_memory_mb
                    continue
                raise ActorRunError("Actor could not be started") from exc

            if run is None:
                raise ActorRunError("Actor run returned no run object")

            log.info(
                "Actor run %s for %r: status=%s memory=%dMB attempt=%d",
                run.id, keyword, run.status, memory_mb, attempt,
            )
            if (
                attempt == 1
                and is_low_memory_failure(run.status, run.status_message, run.exit_code)
                and self._fallback_memory_mb > memory_mb
            ):
                log.warning("Run %s ran out of memory at %dMB; retrying once with %dMB.",
                            run.id, memory_mb, self._fallback_memory_mb)
                memory_mb = self._fallback_memory_mb
                continue
            break

        if run.status in ("RUNNING", "READY"):
            # wait_duration elapsed before the platform timeout fired; stop paying for it.
            log.warning("Run %s still %s after waiting; aborting.", run.id, run.status)
            try:
                await self._client.run(run.id).abort()
            except ApifyApiError as exc:
                log.warning("Abort of run %s failed: %s", run.id, exc.message)

        jobs = await self._read_dataset(run.default_dataset_id, limit)
        if run.status != "SUCCEEDED":
            if jobs:
                log.warning("Run %s ended %s; using %d partial results.", run.id, run.status, len(jobs))
                return jobs
            raise ActorRunError(f"Actor run ended with status {run.status}")
        return jobs

    async def _read_dataset(self, dataset_id: str, limit: int) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        # Dataset order = order the Actor pushed items (the site's listing order).
        async for item in self._client.dataset(dataset_id).iterate_items(limit=limit, clean=True):
            job = actor_mapping.map_item(item)
            if job is None:
                log.debug("Skipping unusable dataset item")
                continue
            if job.url in seen:
                continue
            seen.add(job.url)
            jobs.append(job)
        return jobs
