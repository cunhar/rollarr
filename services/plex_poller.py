"""
services/plex_poller.py
-----------------------
Stateless background daemon thread that polls Plex watch history.

For each watched TV episode found in recent Plex history:
  Unmonitors the episode, deletes its file from disk via Sonarr (if enabled),
  and applies the rolling-window monitoring logic (monitor + search the next N episodes).

For each watched Movie found in recent Plex history:
  Resolves the movie in Radarr, unmonitors it, and deletes its media file from disk.
"""
from __future__ import annotations

import os
import time
import threading
import logging
import datetime

import requests

from integrations.common import now_str as _now, get_plex_credentials
from integrations.sonarr import (
    get_rolling_window,
    find_series_id_by_title,
    get_episodes,
    monitor_episode,
    unmonitor_episode,
    delete_episode_file,
    search_episode,
)
from integrations.radarr import (
    get_radarr_url,
    get_radarr_api_key,
    find_movie_by_title_and_year,
    unmonitor_and_delete_movie,
    unmonitor_movie,
)

from config_store import get_config

logger = logging.getLogger(__name__)

# ── Shared state (read by the Flask UI) ──────────────────────────────────────

poller_state = {
    'enabled':           False,
    'plex_url':          'Not configured',
    'poll_interval':     3600,
    'status':            'starting',   # starting | ok | unreachable | not_configured | polling
    'last_check':        None,
    'next_check':        None,
    'episodes_session':  0,            # media processed total in current session
    'last_episode':      None,         # human-readable last processed item
    'last_error':        None,
}

_state_lock  = threading.Lock()
_wake_event  = threading.Event()
_poll_active = threading.Lock()

# Callback wired by app.py so the poller writes into the shared activity log
_log_callback = None


def set_log_callback(fn):
    global _log_callback
    _log_callback = fn


def _activity_log(status: str, message: str, payload=None):
    logger.info(f"[PlexPoller] {message}")
    if _log_callback:
        _log_callback(status, message, payload)


def _update_state(**kwargs):
    with _state_lock:
        poller_state.update(kwargs)


def get_state() -> dict:
    """Return a thread-safe snapshot of the poller state."""
    plex_url, plex_token = get_plex_credentials()
    poll_interval = int(get_config('PLEX_WATCH_INTERVAL', 3600))

    _update_state(
        enabled=bool(plex_url and plex_token),
        plex_url=plex_url or 'Not configured',
        poll_interval=poll_interval,
    )
    with _state_lock:
        return dict(poller_state)


def trigger_now() -> dict:
    """Trigger an immediate stateless re-check cycle."""
    plex_url, plex_token = get_plex_credentials()

    if not plex_url or not plex_token:
        return {'status': 'error', 'message': 'Plex is not configured'}
    
    logger.info("[PlexPoller] Stateless re-check triggered by user")
    _activity_log('info', "Stateless re-check triggered by user")
    _wake_event.set()
    return {'status': 'success', 'message': 'Re-check cycle triggered'}


# ── Plex API helpers ──────────────────────────────────────────────────────────

def _plex_get(path: str, params: dict = None) -> dict | None:
    plex_url, plex_token = get_plex_credentials()

    if not plex_url or not plex_token:
        return None
    try:
        r = requests.get(
            f"{plex_url}{path}",
            headers={'Accept': 'application/json'},
            params={'X-Plex-Token': plex_token, **(params or {})},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning(f"[PlexPoller] Plex request failed ({path}): {exc}")
        return None


def _get_library_sections() -> list:
    """Return list of library sections from Plex."""
    data = _plex_get('/library/sections')
    if data is None:
        return []
    return data.get('MediaContainer', {}).get('Directory') or []


def _fetch_all_watched_from_library() -> list | None:
    """
    Scan all Plex library sections and return every item with a watched tick mark
    (viewCount > 0), for both Movies and TV episodes.
    Returns None on Plex connectivity failure.
    """
    sections = _get_library_sections()
    if sections is None:
        return None

    watched_items = []
    plex_reachable = False

    for section in sections:
        stype = section.get('type', '')
        skey = section.get('key', '')

        if stype == 'movie':
            # Fetch all watched movies in this section
            data = _plex_get(f'/library/sections/{skey}/all', {
                'type': 1,
                'viewCount>>': 0,
                'X-Plex-Container-Start': 0,
                'X-Plex-Container-Size': 10000,
            })
            if data is not None:
                plex_reachable = True
                for item in (data.get('MediaContainer', {}).get('Metadata') or []):
                    if int(item.get('viewCount', 0)) > 0:
                        item['type'] = 'movie'
                        watched_items.append(item)

        elif stype == 'show':
            # Fetch all watched episodes in this section
            data = _plex_get(f'/library/sections/{skey}/all', {
                'type': 4,
                'viewCount>>': 0,
                'X-Plex-Container-Start': 0,
                'X-Plex-Container-Size': 10000,
            })
            if data is not None:
                plex_reachable = True
                for item in (data.get('MediaContainer', {}).get('Metadata') or []):
                    if int(item.get('viewCount', 0)) > 0:
                        item['type'] = 'episode'
                        watched_items.append(item)

    if not plex_reachable and not sections:
        return None

    return watched_items


def _enrich_metadata(item: dict) -> dict:
    """Fill in missing metadata fields from the full metadata endpoint if incomplete."""
    itype = str(item.get('type', '')).lower()
    if itype in ('4', 'episode'):
        if item.get('grandparentTitle') and item.get('parentIndex') is not None and item.get('index') is not None:
            return item
    elif itype in ('1', 'movie'):
        if item.get('title'):
            return item

    rating_key = item.get('ratingKey')
    if rating_key:
        meta_data = _plex_get(f"/library/metadata/{rating_key}")
        if meta_data:
            hit = (meta_data.get('MediaContainer', {}).get('Metadata') or [None])[0]
            if hit:
                item = {**item, **hit}
    return item


def _plex_delete_item(rating_key):
    """
    Immediately remove an item from the Plex library by its ratingKey.
    Uses DELETE /library/metadata/{ratingKey} which is synchronous and reliable.
    """
    if not rating_key:
        return False
    try:
        plex_url, plex_token = get_plex_credentials()
        if not plex_url or not plex_token:
            return False
        r = requests.delete(
            f"{plex_url}/library/metadata/{rating_key}",
            params={'X-Plex-Token': plex_token},
            headers={'Accept': 'application/json'},
            timeout=10,
        )
        if r.status_code in (200, 204):
            logger.info(f"[PlexPoller] Plex item ratingKey={rating_key} deleted from library")
            return True
        else:
            logger.warning(f"[PlexPoller] Plex delete returned HTTP {r.status_code} for ratingKey={rating_key}")
            return False
    except Exception as exc:
        logger.warning(f"[PlexPoller] Failed to delete Plex item ratingKey={rating_key}: {exc}")
        return False


# ── Sonarr rolling-window logic (Episodes) ────────────────────────────────────

def _process_episode(item: dict) -> tuple[str, str]:
    """Given a Plex history item for a TV episode, unmonitor/delete it and apply the Sonarr rolling window."""
    item        = _enrich_metadata(item)
    show_title  = (item.get('grandparentTitle') or '').strip()
    season_num  = item.get('parentIndex')
    episode_num = item.get('index')

    if not show_title or season_num is None or episode_num is None:
        return 'warning', f"Incomplete metadata for Plex ratingKey {item.get('ratingKey')} — skipped"

    season_num  = int(season_num)
    episode_num = int(episode_num)
    ep_str      = f"S{season_num:02d}E{episode_num:02d}"

    # Resolve in Sonarr
    series_id, series_title = find_series_id_by_title(show_title)
    if not series_id:
        return 'warning', f"{show_title} {ep_str} — not found in Sonarr library, skipping"

    series_title = series_title or show_title

    # Locate episode in Sonarr's list
    episodes = get_episodes(series_id)
    regular  = sorted(
        [e for e in episodes if e.get('seasonNumber', 0) > 0],
        key=lambda e: (e['seasonNumber'], e['episodeNumber']),
    )
    current_idx = next(
        (i for i, e in enumerate(regular)
         if e['seasonNumber'] == season_num and e['episodeNumber'] == episode_num),
        None,
    )

    if current_idx is None:
        return 'warning', f"{series_title} {ep_str} — episode not found in Sonarr"

    current_ep = regular[current_idx]

    # Unmonitor watched episode and delete file from disk if enabled
    delete_enabled = bool(get_config('DELETE_WATCHED_EPISODES', True))
    try:
        unmonitor_episode(current_ep['id'])
    except Exception as exc:
        logger.warning(f"[PlexPoller] Could not unmonitor episode {current_ep['id']}: {exc}")

    ep_file_id = current_ep.get('episodeFileId', 0)
    rating_key = item.get('ratingKey')

    if not ep_file_id or ep_file_id == 0:
        # File already missing from disk — remove item directly from Plex library
        deleted_from_plex = _plex_delete_item(rating_key)
        plex_note = ", removed from Plex" if deleted_from_plex else ", Plex removal failed"
        file_action = f"unmonitored (file already absent from disk{plex_note})"
    elif delete_enabled:
        try:
            delete_episode_file(ep_file_id)
            deleted_from_plex = _plex_delete_item(rating_key)
            plex_note = ", removed from Plex" if deleted_from_plex else ", Plex removal failed"
            file_action = f"unmonitored & file deleted from disk{plex_note}"
        except Exception as exc:
            logger.warning(f"[PlexPoller] Failed deleting episode file ID {ep_file_id}: {exc}")
            file_action = f"unmonitored (file delete failed: {exc})"
    else:
        file_action = "unmonitored (delete disabled)"

    next_eps = regular[current_idx + 1 : current_idx + 1 + get_rolling_window()]

    if not next_eps:
        return 'success', f"{series_title} {ep_str} watched — {file_action}; final episode, nothing to queue"

    newly, already = [], []
    for e in next_eps:
        tag = f"S{e['seasonNumber']:02d}E{e['episodeNumber']:02d}"
        if not e.get('monitored'):
            monitor_episode(e['id'])
            search_episode(e['id'])
            newly.append(tag)
        else:
            already.append(tag)

    return 'success', (
        f"{series_title} {ep_str} watched — {file_action}; "
        f"queued: {newly or 'none'}, already monitored: {already or 'none'}"
    )


# ── Radarr cleanup logic (Movies) ──────────────────────────────────────────────

def _process_movie(item: dict) -> tuple[str, str]:
    """Given a Plex history item for a Movie, unmonitor it and delete its file in Radarr."""
    item   = _enrich_metadata(item)
    title  = (item.get('title') or '').strip()
    year   = item.get('year')

    if not title:
        return 'warning', f"Incomplete movie title for Plex ratingKey {item.get('ratingKey')} — skipped"

    if not get_radarr_url() or not get_radarr_api_key():
        return 'warning', f"Movie '{title}' watched, but Radarr is not configured (RADARR_URL / RADARR_API_KEY missing)"

    movie_id, movie_title = find_movie_by_title_and_year(title, year)
    if not movie_id:
        return 'warning', f"Movie '{title} ({year or ''})' — not found in Radarr library, skipping"

    movie_title = movie_title or title

    # Respect the global delete flag — same setting that governs Sonarr episode cleanup
    delete_enabled = bool(get_config('DELETE_WATCHED_EPISODES', True))
    if delete_enabled:
        _, action_detail = unmonitor_and_delete_movie(movie_id)
    else:
        _, action_detail = unmonitor_movie(movie_id)

    # Remove item directly from Plex library so it won't show up on next scan
    deleted_from_plex = _plex_delete_item(item.get('ratingKey'))
    plex_note = ", removed from Plex" if deleted_from_plex else ", Plex removal failed"

    return 'success', f"Movie '{movie_title}' watched — {action_detail} in Radarr{plex_note}"


# ── Main loop ─────────────────────────────────────────────────────────────────


def _execute_poll() -> int:
    now_ts = _now()
    logger.info("[PlexPoller] Scanning Plex library for all watched items (viewCount > 0)...")
    items = _fetch_all_watched_from_library()

    if items is None:
        msg = "Plex library scan failed — Plex server unreachable or invalid token"
        logger.warning(f"[PlexPoller] {msg}")
        _update_state(status='unreachable', last_check=now_ts, last_error=msg)
        _activity_log('warning', msg)
        return 0

    _update_state(status='ok', last_check=now_ts, last_error=None, last_run_count=0)
    if not items:
        msg = "Plex library scanned — 0 items with watched tick mark found"
        logger.info(f"[PlexPoller] {msg}")
        _activity_log('info', msg)
        return 0

    logger.info(f"[PlexPoller] Found {len(items)} watched item(s) across all libraries.")
    _activity_log('info', f"Plex library scanned — {len(items)} watched item(s) found, processing...")
    
    items_this_run = 0
    with _state_lock:
        processed_count = poller_state.get('episodes_session', 0)
        
    for item in items:
        try:
            itype = str(item.get('type', '')).lower()
            if itype in ('4', 'episode'):
                status, msg = _process_episode(item)
            elif itype in ('1', 'movie'):
                status, msg = _process_movie(item)
            else:
                continue

            processed_count += 1
            items_this_run += 1
            _activity_log(status, msg, {
                'title': item.get('title') or item.get('grandparentTitle'),
                'type':  item.get('type'),
            })
            _update_state(episodes_session=processed_count, last_episode=msg)
        except Exception as exc:
            _activity_log('error', f"Failed processing watched item: {exc}")

    _update_state(
        status='ok',
        episodes_session=processed_count,
        last_run_count=items_this_run,
        last_check=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        last_episode=f"{item.get('grandparentTitle', '')} S{item.get('parentIndex')}E{item.get('index')} - {item.get('title', '')}" if items else None
    )
    return items_this_run


def _poller_loop():
    logger.info(f"[PlexPoller] Starting stateless media poller loop. Delaying 15s to allow Plex to spin up...")
    time.sleep(15)

    while True:
        try:
            plex_url, plex_token = get_plex_credentials()
            poll_interval = int(get_config('PLEX_WATCH_INTERVAL', 3600))

            if not plex_url or not plex_token:
                _update_state(status='not_configured')
                time.sleep(10)
                continue

            with _poll_active:
                try:
                    _execute_poll()
                except Exception as exc:
                    logger.error(f"[PlexPoller] Execution failed: {exc}")

            # Calculate sleep until top-of-the-hour (or interval multiple)
            now_epoch = time.time()
            sleep_sec = poll_interval - int(now_epoch % poll_interval)
            if sleep_sec <= 0:
                sleep_sec = poll_interval

            next_dt = datetime.datetime.now() + datetime.timedelta(seconds=sleep_sec)
            next_ts = next_dt.strftime('%Y-%m-%d %H:%M:%S')
            _update_state(next_check=next_ts)
            
            # Wait until top-of-hour OR until triggered manually
            # Also check for config changes every 2 seconds
            target_time = time.time() + sleep_sec
            while time.time() < target_time:
                if _wake_event.wait(timeout=2):
                    break
                
                current_interval = int(get_config('PLEX_WATCH_INTERVAL', 3600))
                if current_interval != poll_interval:
                    logger.info("[PlexPoller] Watch interval configuration changed. Adjusting sleep.")
                    poll_interval = current_interval
                    
                    # Recalculate sleep to top-of-the-hour or interval multiple
                    now_epoch = time.time()
                    sleep_sec = poll_interval - int(now_epoch % poll_interval)
                    if sleep_sec <= 0:
                        sleep_sec = poll_interval
                        
                    target_time = time.time() + sleep_sec
                    next_dt = datetime.datetime.now() + datetime.timedelta(seconds=sleep_sec)
                    _update_state(poll_interval=poll_interval, next_check=next_dt.strftime('%Y-%m-%d %H:%M:%S'))
            
            _wake_event.clear()
        except Exception as exc:
            logger.error(f"[PlexPoller] Unhandled loop exception: {exc}")
            time.sleep(10)


def start():
    """Start the media poller as a background daemon thread."""
    t = threading.Thread(target=_poller_loop, name='plex-media-poller', daemon=True)
    t.start()
    logger.info("[PlexPoller] Background thread started.")
