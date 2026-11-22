"""
services/media_manager.py
-------------------------
Central media coordinator replacing Sonarr & Radarr workflows:
  - Adding TV shows (TVMaze) & Movies (TMDB) to Rolarr's SQLite catalog
  - Searching NZBgeek, ranking via ReleaseSelector, and queueing Usenet downloads
  - Automatic fallback retry when a release fails & gets blocklisted
  - Direct on-disk episode detection & safe watched-file deletion
"""
from __future__ import annotations

import os
import re
import logging
from typing import Any

from config_store import get_config
from integrations.common import clean_title, sanitize_filename, is_safe_subpath
from integrations import metadata, nzbgeek
from services import db, release_selector, usenet_downloader

logger = logging.getLogger(__name__)

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov', '.wmv'}


def get_rolling_window() -> int:
    return max(1, int(get_config('ROLLING_WINDOW', 3) or 3))


# ── Disk Inspection & Safe Deletion ──────────────────────────────────────────

def _find_show_dir(show_title: str) -> str | None:
    tv_root = get_config('PATH_TV', '/tv') or '/tv'
    if not os.path.isdir(tv_root):
        return None
    direct = os.path.join(tv_root, sanitize_filename(show_title))
    if os.path.isdir(direct):
        return direct
    target_clean = clean_title(show_title)
    for entry in os.listdir(tv_root):
        full = os.path.join(tv_root, entry)
        if os.path.isdir(full) and clean_title(entry) == target_clean:
            return full
    return None


def find_episode_file_on_disk(show_title: str, season: int, episode: int) -> str | None:
    """Locate an episode video file under PATH_TV matching show_title and SxxExx."""
    show_dir = _find_show_dir(show_title)
    if not show_dir:
        return None
    ep_pat = re.compile(rf'(?:^|[\.\s_\-])S0*{int(season)}E0*{int(episode)}(?:$|[\.\s_\-])', re.I)
    for dirpath, _, filenames in os.walk(show_dir):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                continue
            if ep_pat.search(fname):
                return os.path.join(dirpath, fname)
    return None


def episode_exists_on_disk(show_title: str, season: int, episode: int) -> bool:
    return find_episode_file_on_disk(show_title, season, episode) is not None


def _remove_empty_parents(file_path: str, stop_dir: str):
    """Remove empty parent directories up to (but never including) stop_dir."""
    cur = os.path.dirname(file_path)
    real_stop = os.path.realpath(stop_dir)
    while cur and os.path.realpath(cur) != real_stop and is_safe_subpath(cur, stop_dir):
        try:
            if not os.listdir(cur):
                os.rmdir(cur)
                cur = os.path.dirname(cur)
            else:
                break
        except OSError:
            break


def delete_episode_file(
    show_title: str,
    season: int,
    episode: int,
    reported_path: str | None = None,
) -> tuple[bool, str]:
    """Safely delete a watched episode file from PATH_TV."""
    tv_root = get_config('PATH_TV', '/tv') or '/tv'
    target = None

    if reported_path and os.path.isfile(reported_path) and is_safe_subpath(reported_path, tv_root):
        target = reported_path
    else:
        target = find_episode_file_on_disk(show_title, season, episode)

    if not target or not os.path.isfile(target):
        return False, 'file already absent from disk'
    if not is_safe_subpath(target, tv_root):
        return False, f'refused unsafe path outside {tv_root}'

    os.remove(target)
    _remove_empty_parents(target, tv_root)
    logger.info(f"[MediaManager] Deleted episode file: {target}")
    return True, f"deleted {os.path.basename(target)}"


def find_movie_file_on_disk(title: str, year: int | None = None) -> str | None:
    """Locate a movie video file under PATH_MOVIES matching title (and optionally year)."""
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    if not os.path.isdir(movies_root):
        return None
    target_clean = clean_title(title)

    for entry in os.listdir(movies_root):
        full = os.path.join(movies_root, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in VIDEO_EXTS:
            if target_clean in clean_title(entry):
                return full
        elif os.path.isdir(full) and clean_title(entry) == target_clean:
            best = None
            best_sz = 0
            for dirpath, _, fnames in os.walk(full):
                for fn in fnames:
                    if os.path.splitext(fn)[1].lower() in VIDEO_EXTS and 'sample' not in fn.lower():
                        fp = os.path.join(dirpath, fn)
                        sz = os.path.getsize(fp)
                        if sz > best_sz:
                            best_sz = sz
                            best = fp
            if best:
                return best
    return None


def delete_movie_file(
    title: str,
    year: int | None = None,
    reported_path: str | None = None,
) -> tuple[bool, str]:
    """Safely delete a watched movie file (and its empty folder) from PATH_MOVIES."""
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    target = None

    if reported_path and os.path.isfile(reported_path) and is_safe_subpath(reported_path, movies_root):
        target = reported_path
    else:
        target = find_movie_file_on_disk(title, year)

    if not target or not os.path.isfile(target):
        return False, 'file already absent from disk'
    if not is_safe_subpath(target, movies_root):
        return False, f'refused unsafe path outside {movies_root}'

    os.remove(target)
    _remove_empty_parents(target, movies_root)
    logger.info(f"[MediaManager] Deleted movie file: {target}")
    return True, f"deleted {os.path.basename(target)}"


# ── Search, Select & Queue ───────────────────────────────────────────────────

def queue_episode(
    show: dict[str, Any],
    season: int,
    episode: int,
    retry_count: int = 0,
) -> tuple[bool, str]:
    """Search NZBgeek for a single episode, pick the best non-blocklisted release, and enqueue it."""
    show_title = show['title']
    tag = f"S{int(season):02d}E{int(episode):02d}"

    if retry_count == 0:
        if episode_exists_on_disk(show_title, season, episode):
            return False, f"{show_title} {tag} already on disk"
        if db.has_active_download('episode', show_title, season, episode):
            return False, f"{show_title} {tag} already queued"

    candidates = nzbgeek.search_episode(
        show_title=show_title,
        season=season,
        episode=episode,
        tvdb_id=show.get('tvdb_id'),
        imdb_id=show.get('imdb_id'),
    )
    if not candidates:
        return False, f"No NZBgeek results for {show_title} {tag}"

    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='episode',
        expected={'title': show_title, 'season': season, 'episode': episode},
        blocklisted_guids=blocked,
    )
    if not ranked:
        return False, f"No suitable release for {show_title} {tag} after junk/blocklist filter"

    best = ranked[0]
    db.enqueue_download(
        media_type='episode',
        media_id=show.get('id'),
        title=show_title,
        year=show.get('year'),
        season=int(season),
        episode=int(episode),
        nzb_title=best['title'],
        nzb_url=best['nzb_url'],
        nzb_guid=best['guid'],
        size_bytes=best.get('size_bytes', 0),
        retry_count=retry_count,
    )
    usenet_downloader.notify_queue()
    logger.info(f"[MediaManager] Queued {show_title} {tag} -> '{best['title']}' (score={best['score']})")
    return True, f"Queued {best['title']} ({best['parsed']['resolution']}, score {best['score']})"


def queue_movie(movie: dict[str, Any], retry_count: int = 0) -> tuple[bool, str]:
    """Search NZBgeek for a movie, pick the best non-blocklisted release, and enqueue it."""
    title = movie['title']
    year = movie.get('year')

    if retry_count == 0 and db.has_active_download('movie', title):
        return False, f"Movie '{title}' is already queued"

    candidates = nzbgeek.search_movie(
        title=title,
        year=year,
        imdb_id=movie.get('imdb_id'),
    )
    if not candidates:
        return False, f"No NZBgeek results for '{title}'"

    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='movie',
        expected={'title': title, 'year': year},
        blocklisted_guids=blocked,
    )
    if not ranked:
        return False, f"No suitable release for '{title}' after junk/blocklist filter"

    best = ranked[0]
    db.enqueue_download(
        media_type='movie',
        media_id=movie.get('id'),
        title=title,
        year=year,
        nzb_title=best['title'],
        nzb_url=best['nzb_url'],
        nzb_guid=best['guid'],
        size_bytes=best.get('size_bytes', 0),
        retry_count=retry_count,
    )
    if movie.get('id'):
        db.update_movie_status(movie['id'], 'downloading')
    usenet_downloader.notify_queue()
    logger.info(f"[MediaManager] Queued movie '{title}' -> '{best['title']}' (score={best['score']})")
    return True, f"Queued {best['title']} ({best['parsed']['resolution']}, score {best['score']})"


def retry_failed_download(failed_job: dict[str, Any]) -> tuple[bool, str]:
    """Re-search and enqueue the next-best non-blocklisted release after a download failure."""
    next_retry = int(failed_job.get('retry_count') or 0) + 1
    if failed_job['media_type'] == 'episode':
        show = db.get_show(failed_job['media_id']) if failed_job.get('media_id') else None
        if not show:
            show = {'id': None, 'title': failed_job['title'], 'year': failed_job.get('year')}
        return queue_episode(show, failed_job['season'], failed_job['episode'], retry_count=next_retry)
    else:
        movie = db.get_movie(failed_job['media_id']) if failed_job.get('media_id') else None
        if not movie:
            movie = {'id': None, 'title': failed_job['title'], 'year': failed_job.get('year')}
        return queue_movie(movie, retry_count=next_retry)


# ── High-Level Library Operations ────────────────────────────────────────────

def add_show(tvmaze_id: int, start_season: int = 1, start_episode: int = 1) -> dict[str, Any]:
    """Add a TV show from TVMaze and queue the first ROLLING_WINDOW aired episodes."""
    info = metadata.get_show_by_id(tvmaze_id)
    if not info:
        return {'status': 'error', 'message': f'Could not fetch TVMaze show #{tvmaze_id}'}

    show = db.upsert_show(
        tvmaze_id=info['tvmaze_id'],
        tvdb_id=info.get('tvdb_id'),
        imdb_id=info.get('imdb_id'),
        title=info['title'],
        year=info.get('year'),
        poster_url=info.get('poster_url'),
        status='active',
    )

    episodes = metadata.get_show_episodes(tvmaze_id)
    aired = [
        e for e in episodes
        if e['aired'] and (e['season'], e['episode']) >= (int(start_season), int(start_episode))
    ]
    window = aired[:get_rolling_window()]

    queued = []
    skipped = []
    for ep in window:
        tag = f"S{ep['season']:02d}E{ep['episode']:02d}"
        ok, _ = queue_episode(show, ep['season'], ep['episode'])
        if ok:
            queued.append(tag)
        else:
            skipped.append(tag)

    return {
        'status': 'success',
        'message': f"Added '{show['title']}' — queued: {', '.join(queued) if queued else 'none'}",
        'show': show,
        'queued': queued,
        'skipped': skipped,
    }


def add_movie(
    tmdb_id: int | None = None,
    title: str | None = None,
    year: int | None = None,
    imdb_id: str | None = None,
    poster_url: str | None = None,
) -> dict[str, Any]:
    """Add a movie (via TMDB ID or keyless IMDb metadata) and immediately queue from NZBgeek."""
    info = None
    if tmdb_id and int(tmdb_id) > 0:
        info = metadata.get_movie_details(int(tmdb_id))

    if not info:
        if not title:
            return {'status': 'error', 'message': 'Could not resolve movie details'}
        # Derive a stable negative unique key from IMDb ID when no TMDB ID is used
        clean_imdb = re.sub(r'^tt', '', str(imdb_id or ''))
        synthetic_id = -int(clean_imdb) if clean_imdb.isdigit() else -abs(hash(f"{title}:{year}")) % (10**9)
        info = {
            'tmdb_id': synthetic_id,
            'imdb_id': imdb_id or '',
            'title': title,
            'year': int(year) if year else None,
            'poster_url': poster_url or '',
        }

    movie = db.upsert_movie(
        tmdb_id=info['tmdb_id'],
        imdb_id=info.get('imdb_id'),
        title=info['title'],
        year=info.get('year'),
        poster_url=info.get('poster_url'),
        status='wanted',
    )

    ok, msg = queue_movie(movie)
    return {
        'status': 'success' if ok else 'warning',
        'message': f"Added '{movie['title']}' — {msg}",
        'movie': movie,
    }


def resolve_or_track_show(show_title: str) -> dict[str, Any] | None:
    """Find a show in SQLite by title, or auto-resolve & save it via TVMaze."""
    target_clean = clean_title(show_title)
    for s in db.list_shows():
        if clean_title(s['title']) == target_clean:
            return s

    info = metadata.find_show_by_title(show_title)
    if not info:
        return None
    return db.upsert_show(
        tvmaze_id=info['tvmaze_id'],
        tvdb_id=info.get('tvdb_id'),
        imdb_id=info.get('imdb_id'),
        title=info['title'],
        year=info.get('year'),
        poster_url=info.get('poster_url'),
        status='active',
    )
