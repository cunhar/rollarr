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
import struct
import shutil
import time
import datetime
import threading
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


_RE_DISK_EP = re.compile(r'(?:^|[\.\s_\-])S0*(\d{1,2})E0*(\d{1,3})(?:[\-E]E?0*(\d{1,3}))?(?:$|[\.\s_\-])', re.I)


def find_episode_file_on_disk(show_title: str, season: int, episode: int) -> str | None:
    """Locate an episode video file under PATH_TV matching show_title and SxxExx (including multi-episode SxxExx-Eyy)."""
    show_dir = _find_show_dir(show_title)
    if not show_dir:
        return None
    target_s = int(season)
    target_e = int(episode)
    for dirpath, _, filenames in os.walk(show_dir):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                continue
            m = _RE_DISK_EP.search(fname)
            if not m or int(m.group(1)) != target_s:
                continue
            e_start = int(m.group(2))
            e_end = int(m.group(3)) if (m.group(3) and int(m.group(3)) >= e_start and int(m.group(3)) - e_start <= 6) else e_start
            if e_start <= target_e <= e_end:
                return os.path.join(dirpath, fname)
    return None


def episode_exists_on_disk(show_title: str, season: int, episode: int) -> bool:
    return find_episode_file_on_disk(show_title, season, episode) is not None


SIDECAR_EXTS = ('.srt', '.vtt', '.ass', '.ssa', '.sub', '.idx', '.nfo')


def _remap_container_path(reported_path: str | None, local_root: str, remote_prefixes: tuple[str, ...]) -> str | None:
    """Translate a path reported by Jellyfin/Plex (e.g. /data/movies/...) into Rolarr's mount path."""
    if not reported_path:
        return None
    if os.path.exists(reported_path):
        return reported_path
    norm = reported_path.replace('\\', '/')
    for prefix in remote_prefixes:
        p = prefix.rstrip('/') + '/'
        if norm.startswith(p):
            candidate = os.path.join(local_root, norm[len(p):])
            if os.path.exists(candidate):
                return candidate
    return reported_path


def _remove_companion_subtitles(video_path: str, base_dir: str):
    """Remove any sidecar subtitle/metadata files (.srt, .vtt, .nfo, etc.) sharing the same basename stem."""
    parent = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    if not os.path.isdir(parent):
        return
    for fname in os.listdir(parent):
        low = fname.lower()
        if low.endswith(SIDECAR_EXTS) and fname.startswith(stem):
            sub_path = os.path.join(parent, fname)
            if is_safe_subpath(sub_path, base_dir):
                try:
                    os.remove(sub_path)
                except OSError:
                    pass


def _remove_empty_parents(file_path: str, stop_dir: str):
    """Remove empty parent directories up to (but never including) stop_dir."""
    cur = os.path.dirname(file_path)
    real_stop = os.path.realpath(stop_dir)
    while cur and os.path.realpath(cur) != real_stop and is_safe_subpath(cur, stop_dir):
        try:
            entries = os.listdir(cur)
            # If only sidecar/junk files remain with no video files or subdirs, clean them up too
            has_video_or_dir = any(
                os.path.isdir(os.path.join(cur, e)) or os.path.splitext(e)[1].lower() in VIDEO_EXTS
                for e in entries
            )
            if not has_video_or_dir:
                for e in entries:
                    if e.lower().endswith(SIDECAR_EXTS):
                        try:
                            os.remove(os.path.join(cur, e))
                        except OSError:
                            pass
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

    remapped = _remap_container_path(reported_path, tv_root, ('/data/tvshows', '/data/tv', '/tv'))
    if remapped and os.path.isfile(remapped) and is_safe_subpath(remapped, tv_root):
        target = remapped
    else:
        target = find_episode_file_on_disk(show_title, season, episode)

    if not target or not os.path.isfile(target):
        return False, 'file already absent from disk'
    if not is_safe_subpath(target, tv_root):
        return False, f'refused unsafe path outside {tv_root}'

    os.remove(target)
    _remove_companion_subtitles(target, tv_root)
    _remove_empty_parents(target, tv_root)
    logger.info(f"[MediaManager] Deleted episode file: {target}")
    return True, f"deleted {os.path.basename(target)}"


def delete_show_directory(show_title: str) -> tuple[bool, str]:
    """Safely delete an entire TV show directory (and all seasons/episodes/sidecars) from PATH_TV."""
    tv_root = get_config('PATH_TV', '/tv') or '/tv'
    show_dir = _find_show_dir(show_title)
    if show_dir and os.path.isdir(show_dir) and is_safe_subpath(show_dir, tv_root):
        shutil.rmtree(show_dir)
        logger.info(f"[MediaManager] Deleted show directory: {show_dir}")
        return True, f"deleted folder {os.path.basename(show_dir)}"

    deleted_any = False
    for s_num, ep_num in list(_scan_show_episodes_on_disk(show_title)):
        ok, _ = delete_episode_file(show_title, s_num, ep_num)
        if ok:
            deleted_any = True
    return deleted_any, 'deleted episode files' if deleted_any else 'no files on disk'


def _find_movie_dir_on_disk(title: str, year: int | None = None) -> str | None:
    """Locate a movie folder under PATH_MOVIES matching title (and optionally year)."""
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    if not os.path.isdir(movies_root):
        return None
    target_clean = clean_title(title)
    if not target_clean:
        return None

    # 1. Exact clean_title match
    for entry in os.listdir(movies_root):
        full = os.path.join(movies_root, entry)
        if os.path.isdir(full) and clean_title(entry) == target_clean:
            return full

    # 2. Fallback: substring match when year matches (e.g. punctuation/subtitle differences)
    if year:
        year_str = str(year)
        for entry in os.listdir(movies_root):
            full = os.path.join(movies_root, entry)
            if not os.path.isdir(full) or year_str not in entry:
                continue
            entry_clean = clean_title(entry)
            if len(target_clean) >= 3 and (target_clean in entry_clean or entry_clean in target_clean):
                return full
    return None


def find_movie_file_on_disk(title: str, year: int | None = None) -> str | None:
    """Locate a movie video file under PATH_MOVIES matching title (and optionally year)."""
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    if not os.path.isdir(movies_root):
        return None
    target_clean = clean_title(title)
    if not target_clean:
        return None

    movie_dir = _find_movie_dir_on_disk(title, year)
    if movie_dir:
        best = None
        best_sz = -1
        for dirpath, _, fnames in os.walk(movie_dir):
            for fn in fnames:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS and 'sample' not in fn.lower():
                    fp = os.path.join(dirpath, fn)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = 0
                    if sz > best_sz:
                        best_sz = sz
                        best = fp
        if best:
            return best

    for entry in os.listdir(movies_root):
        full = os.path.join(movies_root, entry)
        if os.path.isfile(full) and os.path.splitext(entry)[1].lower() in VIDEO_EXTS:
            if target_clean in clean_title(entry):
                return full
    return None


def delete_movie_file(
    title: str,
    year: int | None = None,
    reported_path: str | None = None,
) -> tuple[bool, str]:
    """Safely delete a movie file and its entire movie folder (including .vtt/.srt sidecars) from PATH_MOVIES."""
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    target = None

    remapped = _remap_container_path(reported_path, movies_root, ('/data/movies', '/movies'))
    if remapped and os.path.isfile(remapped) and is_safe_subpath(remapped, movies_root):
        target = remapped
    else:
        target = find_movie_file_on_disk(title, year)

    movie_dir = _find_movie_dir_on_disk(title, year)
    if not movie_dir and target:
        parent = os.path.dirname(target)
        if os.path.realpath(parent) != os.path.realpath(movies_root) and is_safe_subpath(parent, movies_root):
            movie_dir = parent

    # If the movie lives inside its own folder under PATH_MOVIES, remove the entire folder cleanly
    if movie_dir and os.path.isdir(movie_dir) and is_safe_subpath(movie_dir, movies_root):
        shutil.rmtree(movie_dir)
        logger.info(f"[MediaManager] Deleted movie folder: {movie_dir}")
        return True, f"deleted {os.path.basename(movie_dir)}"

    if not target or not os.path.isfile(target):
        return False, 'file already absent from disk'
    if not is_safe_subpath(target, movies_root):
        return False, f'refused unsafe path outside {movies_root}'

    os.remove(target)
    _remove_companion_subtitles(target, movies_root)
    _remove_empty_parents(target, movies_root)
    logger.info(f"[MediaManager] Deleted movie file: {target}")
    return True, f"deleted {os.path.basename(target)}"


def sync_on_disk_movies() -> list[dict[str, Any]]:
    """
    Scan PATH_MOVIES for any movie directories/files on disk that are not yet tracked in SQLite:
      - Removes empty/video-less orphaned movie directories automatically.
      - Auto-registers any untracked on-disk movie into SQLite so it is visible and deletable in the UI.
    Returns the updated list of tracked movies from SQLite.
    """
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    tracked = db.list_movies()
    if not os.path.isdir(movies_root):
        return tracked

    tracked_clean = {clean_title(m['title']) for m in tracked if m.get('title')}
    tracked_paths = {os.path.realpath(m['file_path']) for m in tracked if m.get('file_path') and os.path.exists(m['file_path'])}
    added_any = False

    for entry in sorted(os.listdir(movies_root)):
        full = os.path.join(movies_root, entry)
        if os.path.isdir(full):
            # Find largest video file inside this movie folder
            best_video = None
            best_sz = -1
            for dirpath, _, fnames in os.walk(full):
                for fn in fnames:
                    if os.path.splitext(fn)[1].lower() in VIDEO_EXTS and 'sample' not in fn.lower():
                        fp = os.path.join(dirpath, fn)
                        try:
                            sz = os.path.getsize(fp)
                        except OSError:
                            sz = 0
                        if sz > best_sz:
                            best_sz = sz
                            best_video = fp
            if not best_video:
                # Orphaned directory with no video files -> remove empty/sidecar-only ghost folder
                if is_safe_subpath(full, movies_root):
                    try:
                        shutil.rmtree(full)
                        logger.info(f"[MediaManager] Cleaned up empty movie directory: {full}")
                    except OSError:
                        pass
                continue

            entry_clean = clean_title(entry)
            if not entry_clean or entry_clean in tracked_clean or os.path.realpath(best_video) in tracked_paths:
                continue
            # Also check substring match against tracked movies with same year
            m_year = re.search(r'[\(\[](\d{4})[\)\]]', entry)
            parsed_year = int(m_year.group(1)) if m_year else None
            raw_title = re.sub(r'\s*[\(\[]\d{4}[\)\]]\s*$', '', entry).strip() or entry

            synthetic_id = -abs(hash(f"disk:{entry_clean}:{parsed_year}")) % (10**9)
            db.upsert_movie(
                tmdb_id=synthetic_id,
                title=raw_title,
                year=parsed_year,
                status='downloaded',
                file_path=best_video,
            )
            tracked_clean.add(entry_clean)
            added_any = True
            logger.info(f"[MediaManager] Auto-imported untracked on-disk movie: '{raw_title}' ({parsed_year}) -> {best_video}")

    return db.list_movies() if added_any else tracked


def sync_on_disk_shows() -> list[dict[str, Any]]:
    """
    Scan PATH_TV for any TV show directories on disk that are not yet tracked in SQLite:
      - Removes empty/video-less orphaned show directories automatically.
      - Auto-registers any untracked on-disk show into SQLite (via TVMaze or local fallback)
        so it is visible and deletable in the UI.
    Returns the updated list of tracked shows from SQLite.
    """
    tv_root = get_config('PATH_TV', '/tv') or '/tv'
    tracked = db.list_shows()
    if not os.path.isdir(tv_root):
        return tracked

    tracked_clean = {clean_title(s['title']) for s in tracked if s.get('title')}
    added_any = False

    for entry in sorted(os.listdir(tv_root)):
        full = os.path.join(tv_root, entry)
        if not os.path.isdir(full):
            continue

        has_video = False
        for _, _, fnames in os.walk(full):
            if any(os.path.splitext(fn)[1].lower() in VIDEO_EXTS for fn in fnames):
                has_video = True
                break

        entry_clean = clean_title(entry)
        if not has_video:
            # Remove empty/sidecar-only untracked TV folders automatically
            if entry_clean not in tracked_clean and is_safe_subpath(full, tv_root):
                try:
                    shutil.rmtree(full)
                    logger.info(f"[MediaManager] Cleaned up empty TV directory: {full}")
                except OSError:
                    pass
            continue

        if not entry_clean or entry_clean in tracked_clean:
            continue

        resolved = resolve_or_track_show(entry)
        if not resolved:
            synthetic_id = -abs(hash(f"disk_tv:{entry_clean}")) % (10**9)
            resolved = db.upsert_show(
                tvmaze_id=synthetic_id,
                title=entry.strip(),
                status='active',
            )
        tracked_clean.add(entry_clean)
        if resolved and resolved.get('title'):
            tracked_clean.add(clean_title(resolved['title']))
        added_any = True
        logger.info(f"[MediaManager] Auto-imported untracked on-disk TV show: '{entry}'")

    return db.list_shows() if added_any else tracked


# ── Search, Select & Queue ───────────────────────────────────────────────────

def _get_episode_airdate(show: dict[str, Any], season: int, episode: int) -> str | None:
    """Look up the airdate ('YYYY-MM-DD') for a show's (season, episode) from TVMaze metadata."""
    tvmaze_id = show.get('tvmaze_id')
    if not tvmaze_id or int(tvmaze_id) <= 0:
        return None
    for ep in metadata.get_show_episodes(int(tvmaze_id)):
        if int(ep['season']) == int(season) and int(ep['episode']) == int(episode):
            return ep.get('airdate') or None
    return None


def queue_episode(
    show: dict[str, Any],
    season: int,
    episode: int,
    retry_count: int = 0,
    airdate: str | None = None,
) -> tuple[bool, str]:
    """Search NZBgeek for a single episode, pick the best non-blocklisted release, and enqueue it."""
    show_title = show['title']
    tag = f"S{int(season):02d}E{int(episode):02d}"

    if retry_count == 0:
        if episode_exists_on_disk(show_title, season, episode):
            return False, f"{show_title} {tag} already on disk"
        if db.has_active_download('episode', show_title, season, episode):
            return False, f"{show_title} {tag} already queued"

    ep_airdate = airdate or _get_episode_airdate(show, season, episode)
    candidates = nzbgeek.search_episode(
        show_title=show_title,
        season=season,
        episode=episode,
        tvdb_id=show.get('tvdb_id'),
        imdb_id=show.get('imdb_id'),
        airdate=ep_airdate,
    )
    if not candidates:
        return False, f"No NZBgeek results for {show_title} {tag}"

    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='episode',
        expected={'title': show_title, 'season': season, 'episode': episode, 'airdate': ep_airdate},
        blocklisted_guids=blocked,
        allow_season_packs=True,
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


def _resolve_movie_imdb_id(movie: dict[str, Any]) -> str:
    """Resolve and persist a missing IMDb ID for a tracked movie via Radarr proxy or IMDb search."""
    existing = (movie.get('imdb_id') or '').strip()
    if existing:
        return existing
    title = (movie.get('title') or '').strip()
    year = movie.get('year')
    tmdb_id = movie.get('tmdb_id')
    if not title:
        return ''
    q = f"{title} {year}".strip() if year else title
    candidates = metadata.search_movies_radarr_proxy(q) or metadata.search_movies_imdb(q)
    target_clean = clean_title(title)
    resolved = ''
    for c in candidates:
        c_imdb = (c.get('imdb_id') or '').strip()
        if not c_imdb:
            continue
        if tmdb_id and int(tmdb_id) > 0 and c.get('tmdb_id') == int(tmdb_id):
            resolved = c_imdb
            break
        if clean_title(c.get('title') or '') == target_clean:
            if not year or not c.get('year') or abs(int(year) - int(c['year'])) <= 1:
                resolved = c_imdb
                break
    if resolved and movie.get('tmdb_id'):
        movie['imdb_id'] = resolved
        db.upsert_movie(
            tmdb_id=movie['tmdb_id'],
            imdb_id=resolved,
            title=movie['title'],
            year=movie.get('year'),
            poster_url=movie.get('poster_url'),
            status=movie.get('status') or 'wanted',
            file_path=movie.get('file_path'),
        )
    return resolved


def queue_movie(movie: dict[str, Any], retry_count: int = 0) -> tuple[bool, str]:
    """Search NZBgeek for a movie, pick the best non-blocklisted release, and enqueue it."""
    title = movie['title']
    year = movie.get('year')

    if retry_count == 0 and db.has_active_download('movie', title):
        return False, f"Movie '{title}' is already queued"

    imdb_id = _resolve_movie_imdb_id(movie)
    candidates = nzbgeek.search_movie(
        title=title,
        year=year,
        imdb_id=imdb_id,
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
    # If IMDB-ID search returned only candidates that got rejected by filters, also try text search
    if not ranked and imdb_id:
        fallback_cands = nzbgeek.search_movie(title=title, year=year, imdb_id=None)
        if fallback_cands:
            ranked = release_selector.select_releases(
                candidates=fallback_cands,
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

def add_show(tvmaze_id: int, auto_queue: bool = False, start_season: int = 1, start_episode: int = 1) -> dict[str, Any]:
    """Add a TV show from TVMaze to the library (untracked/no downloads unless auto_queue=True)."""
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

    if not auto_queue:
        return {
            'status': 'success',
            'message': f"Added '{show['title']}' — open it in Tracked TV Shows to pick an episode to start from",
            'show': show,
            'queued': [],
            'skipped': [],
        }

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


def _scan_show_episode_files_on_disk(show_title: str) -> dict[tuple[int, int], str]:
    """Return a dict mapping (season, episode) -> full file path for on-disk episodes of a show (supports multi-episode SxxExx-Eyy)."""
    found: dict[tuple[int, int], str] = {}
    show_dir = _find_show_dir(show_title)
    if not show_dir:
        return found
    for dirpath, _, filenames in os.walk(show_dir):
        for fname in filenames:
            if os.path.splitext(fname)[1].lower() not in VIDEO_EXTS:
                continue
            m = _RE_DISK_EP.search(fname)
            if m:
                s_num = int(m.group(1))
                e_start = int(m.group(2))
                e_end = int(m.group(3)) if (m.group(3) and int(m.group(3)) >= e_start and int(m.group(3)) - e_start <= 6) else e_start
                full = os.path.join(dirpath, fname)
                for ep_num in range(e_start, e_end + 1):
                    found[(s_num, ep_num)] = full
    return found


def _scan_show_episodes_on_disk(show_title: str) -> set[tuple[int, int]]:
    """Return a set of (season, episode) tuples currently present on disk for a show."""
    return set(_scan_show_episode_files_on_disk(show_title).keys())


_resolution_cache: dict[str, tuple[float, str | None]] = {}


def get_video_resolution(fpath: str | None) -> str | None:
    """Detect video resolution ('2160p', '1080p', '720p', '480p') from filename or MKV/MP4 header."""
    if not fpath or not os.path.isfile(fpath):
        return None
    try:
        mtime = os.path.getmtime(fpath)
    except OSError:
        return None
    cached = _resolution_cache.get(fpath)
    if cached and cached[0] == mtime:
        return cached[1]

    res = None
    parsed = release_selector.parse_release(os.path.basename(fpath))
    if parsed['resolution'] != 'unknown':
        res = parsed['resolution']
    else:
        try:
            w, h = 0, 0
            with open(fpath, 'rb') as f:
                buf = f.read(131072)
                if buf.startswith(b'\x1a\x45\xdf\xa3'):
                    # Matroska EBML PixelWidth (\xb0\x82) and PixelHeight (\xba\x82)
                    m = re.search(b'\xb0\x82(..)(?:.{0,32}?)\xba\x82(..)', buf, re.S)
                    if m:
                        w, h = struct.unpack('>H', m.group(1))[0], struct.unpack('>H', m.group(2))[0]
                    else:
                        m2 = re.search(b'\xba\x82(..)(?:.{0,32}?)\xb0\x82(..)', buf, re.S)
                        if m2:
                            h, w = struct.unpack('>H', m2.group(1))[0], struct.unpack('>H', m2.group(2))[0]
                else:
                    # MP4 top-level box traversal to moov atom
                    sz = os.path.getsize(fpath)
                    pos = 0
                    while pos + 8 <= sz:
                        f.seek(pos)
                        hdr = f.read(16)
                        if len(hdr) < 8:
                            break
                        box_sz, box_type = struct.unpack('>I4s', hdr[:8])
                        if box_sz == 1 and len(hdr) >= 16:
                            box_sz = struct.unpack('>Q', hdr[8:16])[0]
                        elif box_sz == 0:
                            box_sz = sz - pos
                        if box_type == b'moov':
                            moov = f.read(min(box_sz, 524288))
                            m = re.search(b'(?:avc1|hvc1|hev1|vp09|av01)\x00{6}\x00\x01.{16}(..)(..)', moov, re.S)
                            if m:
                                w, h = struct.unpack('>HH', m.group(1) + m.group(2))
                            break
                        if box_sz < 8:
                            break
                        pos += box_sz
            if w >= 3200 or h >= 1800:
                res = '2160p'
            elif w >= 1800 or h >= 1000:
                res = '1080p'
            elif w >= 1200 or h >= 680:
                res = '720p'
            elif w > 0 or h > 0:
                res = '480p'
        except Exception:
            res = None

    _resolution_cache[fpath] = (mtime, res)
    return res


def get_show_on_disk_resolutions(show_title: str, ep_files: dict[tuple[int, int], str] | None = None) -> str | None:
    """Summarize the resolution(s) of on-disk episode files for a show (e.g. '1080p' or '2160p / 1080p')."""
    files_map = ep_files if ep_files is not None else _scan_show_episode_files_on_disk(show_title)
    if not files_map:
        return None
    found_res = {get_video_resolution(fp) for fp in files_map.values()}
    found_res.discard(None)
    if not found_res:
        return None
    order = ['2160p', '1080p', '720p', '480p']
    sorted_res = sorted(found_res, key=lambda r: order.index(r) if r in order else 99)
    return ' / '.join(sorted_res)


def get_show_episodes_status(show_id: int) -> dict[str, Any]:
    """Return all seasons and episodes for a tracked show with on-disk / queued / watched / unaired status."""
    show = db.get_show(show_id)
    if not show:
        return {'status': 'error', 'message': f'Show #{show_id} not found'}

    episodes = metadata.get_show_episodes(show['tvmaze_id'])
    on_disk_files = _scan_show_episode_files_on_disk(show['title'])
    on_disk = set(on_disk_files.keys())
    queued_set = {
        (int(d['season']), int(d['episode']))
        for d in db.list_active_downloads()
        if d['media_type'] == 'episode'
        and clean_title(d['title']) == clean_title(show['title'])
        and d.get('season') is not None
        and d.get('episode') is not None
    }

    # Determine watch watermark & rolling-window horizon
    lw_s = show.get('last_watched_season')
    lw_e = show.get('last_watched_episode')
    explicit_cursor = (int(lw_s), int(lw_e)) if (lw_s is not None and lw_e is not None) else None
    active_eps = on_disk | queued_set
    earliest_active = min(active_eps) if active_eps else None
    latest_active = max(active_eps) if active_eps else None
    window_full = len(active_eps) >= get_rolling_window()

    seasons_map: dict[int, list[dict[str, Any]]] = {}
    for ep in episodes:
        s_num, e_num = ep['season'], ep['episode']
        cur_key = (s_num, e_num)
        ep_res = None
        if cur_key in on_disk:
            ep_status = 'downloaded'
            ep_res = get_video_resolution(on_disk_files.get(cur_key))
        elif cur_key in queued_set:
            ep_status = 'queued'
        elif not ep['aired']:
            ep_status = 'unaired'
        elif (explicit_cursor and cur_key <= explicit_cursor) or (earliest_active and cur_key < earliest_active):
            ep_status = 'watched'
        elif window_full and latest_active and cur_key > latest_active:
            ep_status = 'upcoming'
        else:
            ep_status = 'missing'

        seasons_map.setdefault(s_num, []).append({
            'season': s_num,
            'episode': e_num,
            'title': ep['title'],
            'airdate': ep['airdate'],
            'aired': ep['aired'],
            'status': ep_status,
            'resolution': ep_res,
        })

    seasons = []
    for s_num in sorted(seasons_map.keys(), reverse=True):
        eps = sorted(seasons_map[s_num], key=lambda e: e['episode'], reverse=True)
        seasons.append({
            'season': s_num,
            'total': len(eps),
            'downloaded': sum(1 for e in eps if e['status'] == 'downloaded'),
            'episodes': eps,
        })

    return {'status': 'success', 'show': show, 'seasons': seasons}


def manual_delete_episode(show_id: int, season: int, episode: int) -> dict[str, Any]:
    """
    Manually delete an episode file from disk (and cancel any active download for it)
    without requiring it to be watched in Plex/Jellyfin.
    """
    show = db.get_show(show_id)
    if not show:
        return {'status': 'error', 'message': 'Show not found'}

    season = int(season)
    episode = int(episode)
    tag = f"S{season:02d}E{episode:02d}"

    # Cancel any queued/active download for this episode
    cancelled_dl = False
    for d in db.list_active_downloads():
        if (
            d.get('media_type') == 'episode'
            and clean_title(d.get('title') or '') == clean_title(show['title'])
            and int(d.get('season') or -1) == season
            and int(d.get('episode') or -1) == episode
        ):
            usenet_downloader.cancel_download(int(d['id']))
            cancelled_dl = True

    deleted_disk, detail = delete_episode_file(show['title'], season, episode)
    if not deleted_disk and not cancelled_dl:
        return {'status': 'warning', 'message': f"{show['title']} {tag} — {detail}"}

    if deleted_disk:
        db.update_show_watch_cursor(show['id'], season, episode)

    usenet_downloader._notify_media_servers()
    action = detail if deleted_disk else 'cancelled active download'
    return {'status': 'success', 'message': f"{show['title']} {tag} — {action}"}


def manual_delete_season(show_id: int, season: int) -> dict[str, Any]:
    """Manually delete all on-disk episode files (and cancel queued downloads) for a season."""
    show = db.get_show(show_id)
    if not show:
        return {'status': 'error', 'message': 'Show not found'}

    season = int(season)
    for d in db.list_active_downloads():
        if (
            d.get('media_type') == 'episode'
            and clean_title(d.get('title') or '') == clean_title(show['title'])
            and int(d.get('season') or -1) == season
        ):
            usenet_downloader.cancel_download(int(d['id']))

    on_disk = sorted(ep for (s, ep) in _scan_show_episodes_on_disk(show['title']) if s == season)
    deleted_count = 0
    for ep_num in on_disk:
        ok, _ = delete_episode_file(show['title'], season, ep_num)
        if ok:
            deleted_count += 1

    if on_disk:
        db.update_show_watch_cursor(show['id'], season, max(on_disk))

    usenet_downloader._notify_media_servers()
    return {
        'status': 'success' if deleted_count > 0 else 'warning',
        'message': f"Deleted {deleted_count} episode(s) from {show['title']} Season {season}",
        'deleted_count': deleted_count,
    }


def queue_show_from(show_id: int, start_season: int = 1, start_episode: int = 1) -> dict[str, Any]:
    """Queue the next ROLLING_WINDOW aired episodes for an existing tracked show starting at (S, E)."""
    show = db.get_show(show_id)
    if not show:
        return {'status': 'error', 'message': f'Show #{show_id} not found'}

    episodes = metadata.get_show_episodes(show['tvmaze_id'])
    # Mark everything strictly before (start_season, start_episode) as watched
    prior = [e for e in episodes if (e['season'], e['episode']) < (int(start_season), int(start_episode))]
    if prior:
        last_prior = prior[-1]
        db.update_show_watch_cursor(show['id'], last_prior['season'], last_prior['episode'], force=True)
    aired = [
        e for e in episodes
        if e['aired'] and (e['season'], e['episode']) >= (int(start_season), int(start_episode))
    ]
    window = aired[:get_rolling_window()]
    if not window:
        return {
            'status': 'warning',
            'message': f"No aired episodes found for '{show['title']}' at or after S{int(start_season):02d}E{int(start_episode):02d}",
            'queued': [],
            'skipped': [],
        }

    queued, already_present, not_found = [], [], []
    for ep in window:
        tag = f"S{ep['season']:02d}E{ep['episode']:02d}"
        ok, reason = queue_episode(show, ep['season'], ep['episode'], airdate=ep.get('airdate'))
        if ok:
            queued.append(tag)
        elif 'already on disk' in reason or 'already queued' in reason:
            already_present.append(tag)
        else:
            not_found.append(tag)

    skipped = already_present + not_found
    details = []
    if already_present:
        details.append(f"already present/queued: {', '.join(already_present)}")
    if not_found:
        details.append(f"not on NZBgeek yet: {', '.join(not_found)}")
    detail_suffix = f" ({'; '.join(details)})" if details else ''

    return {
        'status': 'success' if queued else 'warning',
        'message': f"{show['title']} from S{int(start_season):02d}E{int(start_episode):02d} — queued: {', '.join(queued) if queued else 'none'}{detail_suffix}",
        'queued': queued,
        'skipped': skipped,
        'not_found': not_found,
    }


_SHORT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def _fmt_short_date(iso_date: str | None) -> str:
    if not iso_date or len(str(iso_date)) < 10:
        return ''
    try:
        dt = datetime.date.fromisoformat(str(iso_date)[:10])
        return f"{_SHORT_MONTHS[dt.month - 1]} {dt.day}, {dt.year}"
    except ValueError:
        return str(iso_date)[:10]


def get_movie_availability(movie: dict[str, Any], fetch_missing: bool = False) -> dict[str, Any]:
    """
    Determine a movie's theatrical & digital/physical release status:
      - Returns {'available': bool, 'label': str | None, 'stage': str, 'in_cinemas': str | None, 'digital_release': str | None}
      - Pre-release guard: returns available=False if the movie has not yet hit cinemas or is in its
        first 21 theatrical days with no digital/physical release date.
    """
    in_cin = (movie.get('in_cinemas') or '')[:10] or None
    dig_rel = (movie.get('digital_release') or '')[:10] or None

    if fetch_missing and not in_cin and not dig_rel and (movie.get('status') or 'wanted').lower() == 'wanted':
        dates = metadata.get_movie_release_dates(movie.get('tmdb_id'), movie.get('title') or '', movie.get('year'))
        in_cin = dates.get('in_cinemas')
        dig_rel = dates.get('digital_release')
        if (in_cin or dig_rel) and movie.get('id'):
            db.update_movie_release_dates(int(movie['id']), in_cin, dig_rel)
            movie['in_cinemas'] = in_cin
            movie['digital_release'] = dig_rel

    today = datetime.date.today()
    today_str = today.isoformat()

    if dig_rel:
        if dig_rel <= today_str:
            return {
                'available': True,
                'label': f"Digital {_fmt_short_date(dig_rel)}",
                'stage': 'released',
                'in_cinemas': in_cin,
                'digital_release': dig_rel,
            }
        if in_cin and in_cin <= today_str:
            return {
                'available': False,
                'label': f"In Cinemas • Digital {_fmt_short_date(dig_rel)}",
                'stage': 'in_cinemas',
                'in_cinemas': in_cin,
                'digital_release': dig_rel,
            }
        return {
            'available': False,
            'label': f"Digital {_fmt_short_date(dig_rel)}",
            'stage': 'unreleased',
            'in_cinemas': in_cin,
            'digital_release': dig_rel,
        }

    if in_cin:
        if in_cin > today_str:
            return {
                'available': False,
                'label': f"In Cinemas {_fmt_short_date(in_cin)}",
                'stage': 'unreleased',
                'in_cinemas': in_cin,
                'digital_release': None,
            }
        try:
            days_in_cin = (today - datetime.date.fromisoformat(in_cin)).days
        except ValueError:
            days_in_cin = 99
        return {
            'available': days_in_cin >= 21,
            'label': f"In Cinemas ({_fmt_short_date(in_cin)})",
            'stage': 'in_cinemas',
            'in_cinemas': in_cin,
            'digital_release': None,
        }

    year = movie.get('year')
    if year and int(year) > today.year:
        return {
            'available': False,
            'label': f"Unreleased ({year})",
            'stage': 'unreleased',
            'in_cinemas': None,
            'digital_release': None,
        }

    return {
        'available': True,
        'label': None,
        'stage': 'released',
        'in_cinemas': in_cin,
        'digital_release': dig_rel,
    }


def add_movie(
    tmdb_id: int | None = None,
    title: str | None = None,
    year: int | None = None,
    imdb_id: str | None = None,
    poster_url: str | None = None,
) -> dict[str, Any]:
    """Add a movie (via TMDB ID or keyless IMDb metadata) and immediately queue from NZBgeek if released."""
    info = None
    if tmdb_id and int(tmdb_id) > 0:
        info = metadata.get_movie_details(int(tmdb_id))

    if info and imdb_id and not info.get('imdb_id'):
        info['imdb_id'] = imdb_id

    if not info:
        if not title:
            return {'status': 'error', 'message': 'Could not resolve movie details'}
        if tmdb_id and int(tmdb_id) > 0:
            resolved_id = int(tmdb_id)
        else:
            clean_imdb = re.sub(r'^tt', '', str(imdb_id or ''))
            resolved_id = -int(clean_imdb) if clean_imdb.isdigit() else -abs(hash(f"{title}:{year}")) % (10**9)
        info = {
            'tmdb_id': resolved_id,
            'imdb_id': imdb_id or '',
            'title': title,
            'year': int(year) if year else None,
            'poster_url': poster_url or '',
        }

    rel_dates = metadata.get_movie_release_dates(info.get('tmdb_id'), info['title'], info.get('year'))
    existing_file = find_movie_file_on_disk(info['title'], info.get('year'))
    initial_status = 'downloaded' if existing_file else 'wanted'

    movie = db.upsert_movie(
        tmdb_id=info['tmdb_id'],
        imdb_id=info.get('imdb_id'),
        title=info['title'],
        year=info.get('year'),
        poster_url=info.get('poster_url'),
        status=initial_status,
        file_path=existing_file,
        in_cinemas=rel_dates.get('in_cinemas'),
        digital_release=rel_dates.get('digital_release'),
    )

    if existing_file:
        return {
            'status': 'success',
            'message': f"Added '{movie['title']}' — already on disk",
            'movie': movie,
        }

    avail = get_movie_availability(movie, fetch_missing=False)
    if not avail['available']:
        return {
            'status': 'success',
            'message': f"Added '{movie['title']}' — waiting for release ({avail.get('label') or 'Unreleased'})",
            'movie': movie,
        }

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


# ── Manual Release Search, Airing Calendar & Auto-Grabber ────────────────────

def search_releases_for_episode(show_id: int, season: int, episode: int) -> dict[str, Any]:
    """Return top ranked NZBgeek candidates for manual selection (including season packs)."""
    show = db.get_show(show_id)
    if not show:
        return {'status': 'error', 'message': 'Show not found', 'releases': []}
    ep_airdate = _get_episode_airdate(show, season, episode)
    candidates = nzbgeek.search_episode(
        show_title=show['title'],
        season=season,
        episode=episode,
        tvdb_id=show.get('tvdb_id'),
        imdb_id=show.get('imdb_id'),
        airdate=ep_airdate,
        include_season_packs=True,
    )
    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='episode',
        expected={'title': show['title'], 'season': season, 'episode': episode, 'airdate': ep_airdate},
        blocklisted_guids=set(),  # show even blocklisted ones with a flag in manual picker
        allow_season_packs=True,
    )
    out = []
    for r in ranked[:15]:
        out.append({
            'nzb_title': r['title'],
            'nzb_url': r['nzb_url'],
            'nzb_guid': r['guid'],
            'size_bytes': r.get('size_bytes', 0),
            'size_mb': round((r.get('size_bytes') or 0) / (1024 * 1024), 1),
            'resolution': r['parsed']['resolution'],
            'audio': r['parsed']['audio'],
            'source': r['parsed']['source'],
            'is_season_pack': bool(r['parsed'].get('is_season_pack')),
            'score': r['score'],
            'grabs': r.get('grabs', 0),
            'blocklisted': r['guid'] in blocked,
        })
    return {
        'status': 'success',
        'media_type': 'episode',
        'media_id': show['id'],
        'title': show['title'],
        'year': show.get('year'),
        'season': int(season),
        'episode': int(episode),
        'releases': out,
    }


def search_releases_for_movie(movie_id: int) -> dict[str, Any]:
    """Return top ranked NZBgeek candidates for a tracked movie."""
    movie = db.get_movie(movie_id)
    if not movie:
        return {'status': 'error', 'message': 'Movie not found', 'releases': []}
    imdb_id = _resolve_movie_imdb_id(movie)
    candidates = nzbgeek.search_movie(
        title=movie['title'],
        year=movie.get('year'),
        imdb_id=imdb_id,
    )
    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='movie',
        expected={'title': movie['title'], 'year': movie.get('year')},
        blocklisted_guids=set(),
    )
    if not ranked and imdb_id:
        fallback_cands = nzbgeek.search_movie(title=movie['title'], year=movie.get('year'), imdb_id=None)
        if fallback_cands:
            ranked = release_selector.select_releases(
                candidates=fallback_cands,
                media_type='movie',
                expected={'title': movie['title'], 'year': movie.get('year')},
                blocklisted_guids=set(),
            )
    out = []
    for r in ranked[:15]:
        out.append({
            'nzb_title': r['title'],
            'nzb_url': r['nzb_url'],
            'nzb_guid': r['guid'],
            'size_bytes': r.get('size_bytes', 0),
            'size_mb': round((r.get('size_bytes') or 0) / (1024 * 1024), 1),
            'resolution': r['parsed']['resolution'],
            'audio': r['parsed']['audio'],
            'source': r['parsed']['source'],
            'score': r['score'],
            'grabs': r.get('grabs', 0),
            'blocklisted': r['guid'] in blocked,
        })
    return {
        'status': 'success',
        'media_type': 'movie',
        'media_id': movie['id'],
        'title': movie['title'],
        'year': movie.get('year'),
        'releases': out,
    }


def grab_manual_release(data: dict[str, Any]) -> dict[str, Any]:
    """Enqueue a specific user-picked release directly into the Usenet downloader."""
    media_type = data.get('media_type')
    title = data.get('title')
    nzb_title = data.get('nzb_title')
    nzb_url = data.get('nzb_url')
    nzb_guid = data.get('nzb_guid') or nzb_url or nzb_title
    if not all([media_type, title, nzb_title, nzb_url]):
        return {'status': 'error', 'message': 'Missing release parameters'}

    db.enqueue_download(
        media_type=media_type,
        media_id=data.get('media_id'),
        title=title,
        year=data.get('year'),
        season=data.get('season'),
        episode=data.get('episode'),
        nzb_title=nzb_title,
        nzb_url=nzb_url,
        nzb_guid=nzb_guid,
        size_bytes=int(data.get('size_bytes') or 0),
        retry_count=0,
    )
    if media_type == 'movie' and data.get('media_id'):
        db.update_movie_status(int(data['media_id']), 'downloading')
    usenet_downloader.notify_queue()
    return {'status': 'success', 'message': f"Queued '{nzb_title}'"}


def get_airing_this_week(days: int = 7) -> list[dict[str, Any]]:
    """Return the next upcoming episode (at most 1 per show) airing between today and today + `days`."""
    today = datetime.date.today()
    end = today + datetime.timedelta(days=days)
    today_str = today.isoformat()
    end_str = end.isoformat()

    upcoming = []
    for show in db.list_shows():
        eps = metadata.get_show_episodes(show['tvmaze_id'])
        on_disk = _scan_show_episodes_on_disk(show['title'])
        for ep in eps:
            ad = ep.get('airdate') or ''
            if not ad or ad < today_str or ad > end_str:
                continue
            try:
                days_away = (datetime.date.fromisoformat(ad) - today).days
            except ValueError:
                days_away = 0
            upcoming.append({
                'show_id': show['id'],
                'show_title': show['title'],
                'poster_url': show.get('poster_url') or '',
                'season': ep['season'],
                'episode': ep['episode'],
                'ep_title': ep['title'],
                'airdate': ad,
                'days_away': days_away,
                'on_disk': (ep['season'], ep['episode']) in on_disk,
            })
            break

    upcoming.sort(key=lambda x: (x['airdate'], x['show_title'], x['season'], x['episode']))
    return upcoming


_autograb_log_cb = None
_autograb_lock = threading.Lock()


def set_autograb_log_callback(fn):
    global _autograb_log_cb
    _autograb_log_cb = fn


def _ag_log(status: str, msg: str):
    logger.info(f"[AutoGrab] {msg}")
    if _autograb_log_cb:
        try:
            _autograb_log_cb(status, msg)
        except Exception:
            pass


def _try_upgrade_movie(movie: dict[str, Any], cur_res: str, pref_res: str) -> tuple[bool, str]:
    """Search NZBgeek for a strictly higher-resolution release for an already downloaded movie."""
    tiers = release_selector.RES_TIERS
    if cur_res not in tiers or pref_res not in tiers or tiers.index(cur_res) >= tiers.index(pref_res):
        return False, ''
    title = movie['title']
    year = movie.get('year')
    if db.has_active_download('movie', title):
        return False, ''
    imdb_id = _resolve_movie_imdb_id(movie)
    candidates = nzbgeek.search_movie(title=title, year=year, imdb_id=imdb_id)
    if not candidates:
        return False, ''
    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='movie',
        expected={'title': title, 'year': year},
        blocklisted_guids=blocked,
    )
    upgrades = [
        r for r in ranked
        if r['parsed']['resolution'] in tiers
        and tiers.index(r['parsed']['resolution']) > tiers.index(cur_res)
        and tiers.index(r['parsed']['resolution']) <= tiers.index(pref_res)
    ]
    if not upgrades:
        return False, ''
    best = upgrades[0]
    db.enqueue_download(
        media_type='movie',
        media_id=movie.get('id'),
        title=title,
        year=year,
        nzb_title=best['title'],
        nzb_url=best['nzb_url'],
        nzb_guid=best['guid'],
        size_bytes=best.get('size_bytes', 0),
        retry_count=0,
    )
    if movie.get('id'):
        db.update_movie_status(movie['id'], 'downloading')
    usenet_downloader.notify_queue()
    return True, best['parsed']['resolution']


def _try_upgrade_episode(show: dict[str, Any], season: int, episode: int, cur_res: str, pref_res: str) -> tuple[bool, str]:
    """Search NZBgeek for a strictly higher-resolution release for an already downloaded TV episode."""
    tiers = release_selector.RES_TIERS
    if cur_res not in tiers or pref_res not in tiers or tiers.index(cur_res) >= tiers.index(pref_res):
        return False, ''
    show_title = show['title']
    if db.has_active_download('episode', show_title, season, episode):
        return False, ''
    ep_airdate = _get_episode_airdate(show, season, episode)
    candidates = nzbgeek.search_episode(
        show_title=show_title,
        season=season,
        episode=episode,
        tvdb_id=show.get('tvdb_id'),
        imdb_id=show.get('imdb_id'),
        airdate=ep_airdate,
    )
    if not candidates:
        return False, ''
    blocked = db.get_blocklisted_guids()
    ranked = release_selector.select_releases(
        candidates=candidates,
        media_type='episode',
        expected={'title': show_title, 'season': season, 'episode': episode, 'airdate': ep_airdate},
        blocklisted_guids=blocked,
    )
    upgrades = [
        r for r in ranked
        if r['parsed']['resolution'] in tiers
        and tiers.index(r['parsed']['resolution']) > tiers.index(cur_res)
        and tiers.index(r['parsed']['resolution']) <= tiers.index(pref_res)
    ]
    if not upgrades:
        return False, ''
    best = upgrades[0]
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
        retry_count=0,
    )
    usenet_downloader.notify_queue()
    return True, best['parsed']['resolution']


def run_auto_grab() -> dict[str, Any]:
    """
    Periodic auto-grabber:
      1. Skips if Disk Space Guard is active or NZBgeek/NNTP not configured.
      2. Searches & queues WANTED movies (skipping pre-release/unreleased movies),
         and checks DOWNLOADED movies below PREFERRED_RESOLUTION for quality upgrades.
      3. For active TV shows (with at least 1 episode on disk or a watch cursor),
         queues newly aired episodes to keep ROLLING_WINDOW full and checks on-disk
         episodes below PREFERRED_RESOLUTION for quality upgrades.
    """
    if not _autograb_lock.acquire(blocking=False):
        return {'status': 'info', 'message': 'Auto-grab already running'}

    try:
        from services.disk_service import check_disk_space_guard
        low_disk, low_reason = check_disk_space_guard()
        if low_disk:
            _ag_log('warning', f"Auto-grab skipped — {low_reason}")
            return {'status': 'warning', 'message': f"Skipped: {low_reason}"}

        if not nzbgeek.get_nzbgeek_api_key() or not usenet_downloader.is_nntp_configured():
            return {'status': 'info', 'message': 'NZBgeek or NNTP not configured'}

        cur_year = datetime.date.today().year
        pref_res = (get_config('PREFERRED_RESOLUTION', '1080p') or '1080p').lower()
        tiers = release_selector.RES_TIERS
        queued_items: list[str] = []

        # 1. Check WANTED movies (with pre-release guard) & DOWNLOADED movies for quality upgrades
        for m in db.list_movies():
            st = (m.get('status') or '').lower()
            if st == 'wanted':
                if m.get('year') and int(m['year']) > cur_year:
                    continue
                if db.has_active_download('movie', m['title']):
                    continue
                avail = get_movie_availability(m, fetch_missing=True)
                if not avail['available']:
                    continue
                ok, _ = queue_movie(m)
                if ok:
                    queued_items.append(f"Movie: {m['title']}")
            elif st == 'downloaded' and pref_res in tiers:
                fpath = m.get('file_path')
                disk_path = fpath if (fpath and os.path.isfile(fpath)) else find_movie_file_on_disk(m['title'], m.get('year'))
                cur_res = get_video_resolution(disk_path) if disk_path else None
                if cur_res in tiers and tiers.index(cur_res) < tiers.index(pref_res):
                    ok_up, new_res = _try_upgrade_movie(m, cur_res, pref_res)
                    if ok_up:
                        queued_items.append(f"Movie Upgrade: {m['title']} ({cur_res} -> {new_res})")

        # 2. Check active TV shows for newly aired episodes & quality upgrades within rolling window
        window_size = get_rolling_window()
        for show in db.list_shows():
            on_disk_files = _scan_show_episode_files_on_disk(show['title'])
            on_disk = set(on_disk_files.keys())
            lw_s, lw_e = show.get('last_watched_season'), show.get('last_watched_episode')
            explicit_cursor = (int(lw_s), int(lw_e)) if (lw_s is not None and lw_e is not None) else None

            # Only auto-grab shows the user has actually started (on disk or watch cursor set)
            if not on_disk and not explicit_cursor:
                continue

            # Check existing on-disk episodes for quality upgrades
            if pref_res in tiers:
                for (s_num, e_num), fp in sorted(on_disk_files.items()):
                    cur_res = get_video_resolution(fp)
                    if cur_res in tiers and tiers.index(cur_res) < tiers.index(pref_res):
                        ok_up, new_res = _try_upgrade_episode(show, s_num, e_num, cur_res, pref_res)
                        if ok_up:
                            queued_items.append(f"{show['title']} S{s_num:02d}E{e_num:02d} ({cur_res} -> {new_res})")

            queued_set = {
                (int(d['season']), int(d['episode']))
                for d in db.list_active_downloads()
                if d['media_type'] == 'episode'
                and clean_title(d['title']) == clean_title(show['title'])
                and d.get('season') is not None and d.get('episode') is not None
            }
            active_eps = on_disk | queued_set
            needed = window_size - len(active_eps)
            if needed <= 0:
                continue

            watermark = max(active_eps) if active_eps else explicit_cursor
            eps = metadata.get_show_episodes(show['tvmaze_id'], force_refresh=True)
            candidates = [
                e for e in eps
                if e['aired'] and (e['season'], e['episode']) > watermark
            ][:needed]
            for ep in candidates:
                ok, _ = queue_episode(show, ep['season'], ep['episode'])
                if ok:
                    queued_items.append(f"{show['title']} S{ep['season']:02d}E{ep['episode']:02d}")

        if queued_items:
            msg = f"Auto-grab queued {len(queued_items)} item(s): {', '.join(queued_items)}"
            _ag_log('success', msg)
        else:
            msg = "Auto-grab completed — nothing new to queue"
            logger.info(f"[AutoGrab] {msg}")
        return {'status': 'success', 'message': msg, 'queued': queued_items}
    finally:
        _autograb_lock.release()


def _autograb_loop():
    logger.info("[AutoGrab] Background thread started (initial delay 90s).")
    time.sleep(90)
    while True:
        try:
            hours = int(get_config('AUTO_GRAB_INTERVAL_HOURS', 6) or 0)
            if hours <= 0:
                time.sleep(60)
                continue
            run_auto_grab()
            time.sleep(max(1, hours) * 3600)
        except Exception as exc:
            logger.error(f"[AutoGrab] Loop error: {exc}")
            time.sleep(300)


def start_autograb():
    t = threading.Thread(target=_autograb_loop, name='rolarr-autograb', daemon=True)
    t.start()
