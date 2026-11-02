"""SQLite persistence: keyword cache, per-user sent history and Show More sessions.

All queries are small and indexed, so they run inline on the event loop.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .models import Job

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keyword_cache (
    keyword    TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    jobs_json  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sent_jobs (
    user_id INTEGER NOT NULL,
    url     TEXT NOT NULL,
    sent_at REAL NOT NULL,
    PRIMARY KEY (user_id, url)
);
CREATE TABLE IF NOT EXISTS last_sent_by_source (
    user_id    INTEGER NOT NULL,
    keyword    TEXT NOT NULL,
    source     TEXT NOT NULL,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, keyword, source)
);
CREATE TABLE IF NOT EXISTS more_sessions (
    session_id TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    keyword    TEXT NOT NULL,
    jobs_json  TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_more_sessions_user ON more_sessions (user_id, keyword);
CREATE TABLE IF NOT EXISTS rate_events (
    kind    TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_events ON rate_events (kind, user_id, at);
CREATE INDEX IF NOT EXISTS idx_rate_events_kind_at ON rate_events (kind, at);
"""


@dataclass(frozen=True)
class CachedJobs:
    jobs: list[Job]
    fetched_at: float


@dataclass(frozen=True)
class LastSent:
    url: str
    title: str


def _dump(jobs: Iterable[Job]) -> str:
    return json.dumps([j.to_dict() for j in jobs], ensure_ascii=False)


def _load(raw: str) -> list[Job]:
    return [Job.from_dict(d) for d in json.loads(raw)]


class Storage:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- result cache (shared by all users; key is "<site>|<keyword>") ------

    def get_cached_jobs(self, keyword: str, ttl_secs: float) -> CachedJobs | None:
        """Return the cached result set if it is younger than ttl_secs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT fetched_at, jobs_json FROM keyword_cache WHERE keyword = ?", (keyword,)
            ).fetchone()
        if row is None or self._clock() - row["fetched_at"] >= ttl_secs:
            return None
        return CachedJobs(jobs=_load(row["jobs_json"]), fetched_at=row["fetched_at"])

    def save_cached_jobs(self, keyword: str, jobs: list[Job]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO keyword_cache (keyword, fetched_at, jobs_json) VALUES (?, ?, ?) "
                "ON CONFLICT(keyword) DO UPDATE SET fetched_at = excluded.fetched_at, jobs_json = excluded.jobs_json",
                (keyword, self._clock(), _dump(jobs)),
            )

    # --- per-user history ---------------------------------------------------

    def sent_urls(self, user_id: int) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT url FROM sent_jobs WHERE user_id = ?", (user_id,)).fetchall()
        return {r["url"] for r in rows}

    def mark_sent(self, user_id: int, jobs: Iterable[Job]) -> None:
        now = self._clock()
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO sent_jobs (user_id, url, sent_at) VALUES (?, ?, ?)",
                [(user_id, j.url, now) for j in jobs],
            )

    def get_last_sent_map(self, user_id: int, keyword: str) -> dict[str | None, LastSent]:
        """Most recent job sent to this user for this keyword, per site."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, url, title FROM last_sent_by_source WHERE user_id = ? AND keyword = ?",
                (user_id, keyword),
            ).fetchall()
        return {(r["source"] or None): LastSent(url=r["url"], title=r["title"]) for r in rows}

    def set_last_sent(self, user_id: int, keyword: str, job: Job) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO last_sent_by_source (user_id, keyword, source, url, title, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, keyword, source) DO UPDATE SET url = excluded.url, title = excluded.title, "
                "updated_at = excluded.updated_at",
                (user_id, keyword, job.source or "", job.url, job.title, self._clock()),
            )

    # --- Show More sessions -------------------------------------------------

    def retire_more_sessions(self, user_id: int, keyword: str) -> None:
        """Expire unused buttons so a stale one cannot resend jobs a newer search covered."""
        with self._lock:
            self._conn.execute(
                "UPDATE more_sessions SET consumed = 1 WHERE user_id = ? AND keyword = ? AND consumed = 0",
                (user_id, keyword),
            )

    def create_more_session(self, user_id: int, keyword: str, jobs: list[Job]) -> str:
        """Store the remaining jobs and return a short id for the callback data."""
        session_id = secrets.token_urlsafe(8)  # 11 chars
        with self._lock:
            self._conn.execute(
                "INSERT INTO more_sessions (session_id, user_id, keyword, jobs_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, keyword, _dump(jobs), self._clock()),
            )
        return session_id

    def claim_more_session(self, session_id: str, user_id: int, max_age_secs: float) -> tuple[str, list[Job]] | None:
        """Atomically mark a session used and return (keyword, jobs).

        Returns None if the session is unknown, belongs to another user, is too
        old, or was already claimed (e.g. a double click).
        """
        min_created = self._clock() - max_age_secs
        with self._lock:
            cur = self._conn.execute(
                "UPDATE more_sessions SET consumed = 1 "
                "WHERE session_id = ? AND user_id = ? AND consumed = 0 AND created_at > ? "
                "RETURNING keyword, jobs_json",
                (session_id, user_id, min_created),
            )
            rows = cur.fetchall()  # drain so the statement completes and commits
        if not rows:
            return None
        return rows[0]["keyword"], _load(rows[0]["jobs_json"])

    # --- rate limiting -------------------------------------------------------

    def now(self) -> float:
        return self._clock()

    def add_rate_event(self, kind: str, user_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rate_events (kind, user_id, at) VALUES (?, ?, ?)", (kind, user_id, self._clock())
            )

    def rate_events_since(self, kind: str, since: float, user_id: int | None = None) -> tuple[int, float | None]:
        """(count, oldest timestamp) of events newer than `since`; user_id=None counts all users."""
        sql = "SELECT COUNT(*) AS n, MIN(at) AS oldest FROM rate_events WHERE kind = ? AND at > ?"
        params: tuple = (kind, since)
        if user_id is not None:
            sql += " AND user_id = ?"
            params += (user_id,)
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row["n"], row["oldest"]

    def last_rate_event(self, kind: str, user_id: int) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(at) AS last FROM rate_events WHERE kind = ? AND user_id = ?", (kind, user_id)
            ).fetchone()
        return row["last"]

    def purge_rate_events(self, max_age_secs: float) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM rate_events WHERE at <= ?", (self._clock() - max_age_secs,))
        return cur.rowcount

    def purge_old_sessions(self, max_age_secs: float) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM more_sessions WHERE created_at <= ?", (self._clock() - max_age_secs,)
            )
        return cur.rowcount
