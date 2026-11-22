"""
services/db.py
--------------
Lightweight thread-safe SQLite storage (/config/rolarr.db) for:
  - tracked TV shows (TVMaze)
  - tracked Movies (TMDB)
  - persistent Usenet download queue & history
  - failed-release blocklist (for automatic retry of next-best NZB)
"""
from __future__ import annotations

import os
import sqlite3
import threading
import logging
from contextlib import contextmanager
from typing import Any

from integrations.common import now_str

logger = logging.getLogger(__name__)

CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.path.join(CONFIG_DIR, 'rolarr.db')
_db_lock = threading.RLock()


def get_db_path() -> str:
    return DB_PATH


def set_db_path(path: str):
    """Override database path (primarily for unit tests)."""
    global DB_PATH
    DB_PATH = path


@contextmanager
def _conn():
    with _db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA journal_mode=WAL;')
            conn.execute('PRAGMA foreign_keys=ON;')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db():
    """Create tables if they do not already exist."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS shows (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tvmaze_id   INTEGER UNIQUE NOT NULL,
                tvdb_id     INTEGER,
                imdb_id     TEXT,
                title       TEXT NOT NULL,
                year        INTEGER,
                poster_url  TEXT,
                status      TEXT DEFAULT 'active',
                added_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS movies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id     INTEGER UNIQUE NOT NULL,
                imdb_id     TEXT,
                title       TEXT NOT NULL,
                year        INTEGER,
                poster_url  TEXT,
                status      TEXT DEFAULT 'wanted',
                file_path   TEXT,
                added_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS downloads (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type       TEXT NOT NULL,      -- 'episode' | 'movie'
                media_id         INTEGER,            -- shows.id or movies.id
                title            TEXT NOT NULL,      -- Show title or Movie title
                year             INTEGER,
                season           INTEGER,
                episode          INTEGER,
                nzb_title        TEXT NOT NULL,
                nzb_url          TEXT NOT NULL,
                nzb_guid         TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'queued',  -- queued|downloading|verifying|unpacking|completed|failed|cancelled
                progress_pct     REAL DEFAULT 0.0,
                size_bytes       INTEGER DEFAULT 0,
                downloaded_bytes INTEGER DEFAULT 0,
                retry_count      INTEGER DEFAULT 0,
                error            TEXT,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS blocklist (
                nzb_guid   TEXT PRIMARY KEY,
                nzb_title  TEXT,
                reason     TEXT,
                failed_at  TEXT NOT NULL
            );
        """)
    logger.info(f"[DB] Initialized SQLite database at {DB_PATH}")


# ── Shows ────────────────────────────────────────────────────────────────────

def upsert_show(
    tvmaze_id: int,
    title: str,
    tvdb_id: int | None = None,
    imdb_id: str | None = None,
    year: int | None = None,
    poster_url: str | None = None,
    status: str = 'active',
) -> dict[str, Any]:
    ts = now_str()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO shows (tvmaze_id, tvdb_id, imdb_id, title, year, poster_url, status, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tvmaze_id) DO UPDATE SET
                tvdb_id    = COALESCE(excluded.tvdb_id, shows.tvdb_id),
                imdb_id    = COALESCE(excluded.imdb_id, shows.imdb_id),
                title      = excluded.title,
                year       = COALESCE(excluded.year, shows.year),
                poster_url = COALESCE(excluded.poster_url, shows.poster_url),
                status     = excluded.status
            """,
            (tvmaze_id, tvdb_id, imdb_id, title, year, poster_url, status, ts),
        )
        row = c.execute("SELECT * FROM shows WHERE tvmaze_id = ?", (tvmaze_id,)).fetchone()
        return dict(row)


def list_shows() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM shows ORDER BY title COLLATE NOCASE ASC").fetchall()
        return [dict(r) for r in rows]


def get_show(show_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
        return dict(row) if row else None


def get_show_by_tvmaze(tvmaze_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM shows WHERE tvmaze_id = ?", (tvmaze_id,)).fetchone()
        return dict(row) if row else None


def delete_show(show_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM shows WHERE id = ?", (show_id,))
        return cur.rowcount > 0


# ── Movies ───────────────────────────────────────────────────────────────────

def upsert_movie(
    tmdb_id: int,
    title: str,
    imdb_id: str | None = None,
    year: int | None = None,
    poster_url: str | None = None,
    status: str = 'wanted',
    file_path: str | None = None,
) -> dict[str, Any]:
    ts = now_str()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO movies (tmdb_id, imdb_id, title, year, poster_url, status, file_path, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tmdb_id) DO UPDATE SET
                imdb_id    = COALESCE(excluded.imdb_id, movies.imdb_id),
                title      = excluded.title,
                year       = COALESCE(excluded.year, movies.year),
                poster_url = COALESCE(excluded.poster_url, movies.poster_url),
                status     = excluded.status,
                file_path  = COALESCE(excluded.file_path, movies.file_path)
            """,
            (tmdb_id, imdb_id, title, year, poster_url, status, file_path, ts),
        )
        row = c.execute("SELECT * FROM movies WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
        return dict(row)


def list_movies() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM movies ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_movie(movie_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
        return dict(row) if row else None


def update_movie_status(movie_id: int, status: str, file_path: str | None = None):
    with _conn() as c:
        if file_path is not None:
            c.execute("UPDATE movies SET status = ?, file_path = ? WHERE id = ?", (status, file_path, movie_id))
        else:
            c.execute("UPDATE movies SET status = ? WHERE id = ?", (status, movie_id))


def mark_movie_watched_by_title(title: str, year: int | None = None):
    with _conn() as c:
        if year is not None:
            c.execute(
                "UPDATE movies SET status = 'watched' WHERE LOWER(title) = LOWER(?) AND (year = ? OR year IS NULL)",
                (title, int(year)),
            )
        else:
            c.execute("UPDATE movies SET status = 'watched' WHERE LOWER(title) = LOWER(?)", (title,))


def delete_movie(movie_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
        return cur.rowcount > 0


# ── Downloads ────────────────────────────────────────────────────────────────

ACTIVE_DOWNLOAD_STATUSES = ('queued', 'downloading', 'verifying', 'unpacking')


def enqueue_download(
    media_type: str,
    media_id: int | None,
    title: str,
    nzb_title: str,
    nzb_url: str,
    nzb_guid: str,
    size_bytes: int = 0,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    ts = now_str()
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO downloads (
                media_type, media_id, title, year, season, episode,
                nzb_title, nzb_url, nzb_guid, status, progress_pct,
                size_bytes, downloaded_bytes, retry_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0.0, ?, 0, ?, ?, ?)
            """,
            (
                media_type, media_id, title, year, season, episode,
                nzb_title, nzb_url, nzb_guid, int(size_bytes or 0),
                int(retry_count), ts, ts,
            ),
        )
        row = c.execute("SELECT * FROM downloads WHERE id = ?", (cur.lastrowid,)).fetchone()
        return dict(row)


def has_active_download(media_type: str, title: str, season: int | None = None, episode: int | None = None) -> bool:
    placeholders = ','.join('?' for _ in ACTIVE_DOWNLOAD_STATUSES)
    with _conn() as c:
        if media_type == 'episode':
            row = c.execute(
                f"SELECT 1 FROM downloads WHERE media_type = 'episode' AND LOWER(title) = LOWER(?) "
                f"AND season = ? AND episode = ? AND status IN ({placeholders}) LIMIT 1",
                (title, season, episode, *ACTIVE_DOWNLOAD_STATUSES),
            ).fetchone()
        else:
            row = c.execute(
                f"SELECT 1 FROM downloads WHERE media_type = 'movie' AND LOWER(title) = LOWER(?) "
                f"AND status IN ({placeholders}) LIMIT 1",
                (title, *ACTIVE_DOWNLOAD_STATUSES),
            ).fetchone()
        return row is not None


def get_next_queued_download() -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM downloads WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def reset_interrupted_downloads():
    """On startup, re-queue any jobs that were mid-flight when the container stopped."""
    with _conn() as c:
        c.execute(
            "UPDATE downloads SET status = 'queued', progress_pct = 0.0, downloaded_bytes = 0, updated_at = ? "
            "WHERE status IN ('downloading', 'verifying', 'unpacking')",
            (now_str(),),
        )


def update_download(download_id: int, **fields):
    if not fields:
        return
    fields['updated_at'] = now_str()
    cols = ', '.join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [download_id]
    with _conn() as c:
        c.execute(f"UPDATE downloads SET {cols} WHERE id = ?", vals)


def get_download(download_id: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
        return dict(row) if row else None


def list_active_downloads() -> list[dict[str, Any]]:
    placeholders = ','.join('?' for _ in ACTIVE_DOWNLOAD_STATUSES)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM downloads WHERE status IN ({placeholders}) ORDER BY id ASC",
            ACTIVE_DOWNLOAD_STATUSES,
        ).fetchall()
        return [dict(r) for r in rows]


def list_recent_downloads(limit: int = 20) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM downloads ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Blocklist ────────────────────────────────────────────────────────────────

def add_to_blocklist(nzb_guid: str, nzb_title: str = '', reason: str = ''):
    if not nzb_guid:
        return
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO blocklist (nzb_guid, nzb_title, reason, failed_at)
            VALUES (?, ?, ?, ?)
            """,
            (nzb_guid, nzb_title, reason, now_str()),
        )


def get_blocklisted_guids() -> set[str]:
    with _conn() as c:
        rows = c.execute("SELECT nzb_guid FROM blocklist").fetchall()
        return {r['nzb_guid'] for r in rows}
