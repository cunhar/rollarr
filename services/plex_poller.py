"""
services/plex_poller.py
-----------------------
Background daemon thread that polls Plex watch history.
By default, polls at the top of every hour (HH:00:00).

For each newly-watched TV episode found:
  Resolves the series in Sonarr and applies the rolling-window monitoring logic
  (monitor + search the next N episodes).

For each newly-watched Movie found:
  Resolves the movie in Radarr, unmonitors it, and deletes its media file from disk.

State persistence
-----------------
A high-water mark (last processed viewedAt Unix timestamp) and total items processed
counter are saved to CONFIG_DIR/plex_poll_state.json so stats persist across container restarts.
"""
from __future__ import annotations

import os
import json
import time
import threading
import logging
import datetime

import requests

from integrations.sonarr import (
    get_rolling_window,
    find_series_id_by_title,
    get_episodes,
    monitor_episode,
    search_episode,
)
from integrations.radarr import (
    find_movie_by_title_and_year,
    unmonitor_and_delete_movie,
)

from config_store import get_config

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

STATE_FILE = os.path.join(CONFIG_DIR, 'plex_poll_state.json')

# ── Shared state (read by the Flask UI) ──────────────────────────────────────

poller_state = {
    'enabled':           False,
    'plex_url':          'Not configured',
    'poll_interval':     3600,
    'status':            'starting',   # starting | ok | unreachable | not_configured | polling
    'last_check':        None,
    'next_check':        None,
    'episodes_session':  0,            # media processed total (persisted)
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
    plex_url = (get_config('PLEX_URL') or '').rstrip('/')
    plex_token = get_config('PLEX_TOKEN') or ''
    poll_interval = int(get_config('PLEX_WATCH_INTERVAL', 3600))

    _update_state(
        enabled=bool(plex_url and plex_token),
        plex_url=plex_url or 'Not configured',
        poll_interval=poll_interval,
    )
    with _state_lock:
        return dict(poller_state)


def trigger_now() -> dict:
    """Trigger a manual poll cycle immediately."""
    plex_url = (get_config('PLEX_URL') or '').rstrip('/')
    plex_token = get_config('PLEX_TOKEN') or ''

    if not plex_url or not plex_token:
        return {'status': 'error', 'message': 'Plex is not configured'}
    
    logger.info("[PlexPoller] Manual poll triggered by user")
    _wake_event.set()
    return {'status': 'success', 'message': 'Poll cycle triggered'}


def reset_counter() -> dict:
    """Reset the items processed counter to 0."""
    watermark, _ = _load_persisted_state()
    _save_persisted_state(watermark, 0)
    _update_state(episodes_session=0)
    logger.info("[PlexPoller] Items processed counter reset to 0 by user.")
    return {'status': 'success', 'message': 'Counter reset to 0'}


# ── State persistence ─────────────────────────────────────────────────────────

def _load_persisted_state() -> tuple[int, int]:
    """Return (last_viewed_at, items_processed) from STATE_FILE."""
    try:
        with open(STATE_FILE, 'r') as f:
            data = json.load(f)
            return int(data.get('last_viewed_at', 0)), int(data.get('items_processed', 0))
    except Exception:
        return 0, 0


def _save_persisted_state(ts: int, count: int):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({'last_viewed_at': ts, 'items_processed': count}, f)
    except Exception as e:
        logger.warning(f"[PlexPoller] Could not save persisted state: {e}")


# ── Plex API helpers ──────────────────────────────────────────────────────────

def _plex_get(path: str, params: dict = None) -> dict | None:
    plex_url = (get_config('PLEX_URL') or '').rstrip('/')
    plex_token = get_config('PLEX_TOKEN') or ''

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


def _fetch_new_watched(since_ts: int) -> list | None:
    """Return watch history items with viewedAt > since_ts, sorted oldest-first."""
    data = _plex_get('/status/sessions/history/all', {
        'sort': 'viewedAt:asc',
        'X-Plex-Container-Start': 0,
        'X-Plex-Container-Size': 200,
    })
    if data is None:
        return None
    items = data.get('MediaContainer', {}).get('Metadata') or []
    valid_items = []
    for i in items:
        itype = i.get('type')
        if itype in (1, 4, 'movie', 'episode') and int(i.get('viewedAt', 0)) > since_ts:
            valid_items.append(i)
    return valid_items


def _enrich_metadata(item: dict) -> dict:
    """Fill in missing metadata fields from the full metadata endpoint if incomplete."""
    itype = item.get('type')
    if itype in (4, 'episode'):
        if item.get('grandparentTitle') and item.get('parentIndex') is not None and item.get('index') is not None:
            return item
    elif itype in (1, 'movie'):
        if item.get('title'):
            return item

    meta_data = _plex_get(f"/library/metadata/{item['ratingKey']}")
    if meta_data:
        hit = (meta_data.get('MediaContainer', {}).get('Metadata') or [None])[0]
        if hit:
            item = {**item, **hit}
    return item


# ── Sonarr rolling-window logic (Episodes) ────────────────────────────────────

def _process_episode(item: dict) -> tuple[str, str]:
    """Given a Plex history item for a TV episode, apply the Sonarr rolling window."""
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

    next_eps = regular[current_idx + 1 : current_idx + 1 + get_rolling_window()]

    if not next_eps:
        return 'success', f"{series_title} {ep_str} watched — final episode, nothing to queue"

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
        f"{series_title} {ep_str} watched — "
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

    if not RADARR_URL or not RADARR_API_KEY:
        return 'warning', f"Movie '{title}' watched, but Radarr is not configured (RADARR_URL / RADARR_API_KEY missing)"

    movie_id, movie_title = find_movie_by_title_and_year(title, year)
    if not movie_id:
        return 'warning', f"Movie '{title} ({year or ''})' — not found in Radarr library, skipping"

    movie_title = movie_title or title
    _, action_detail = unmonitor_and_delete_movie(movie_id)

    return 'success', f"Movie '{movie_title}' watched — {action_detail} in Radarr"


# ── Main loop ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _execute_poll(watermark: int, media_total: int) -> tuple[int, int]:
    now_ts = _now()
    logger.info(f"[PlexPoller] Checking Plex history (since viewedAt={watermark})")
    new_items = _fetch_new_watched(watermark)

    if new_items is None:
        logger.warning("[PlexPoller] Plex unreachable.")
        _update_state(status='unreachable', last_check=now_ts)
    else:
        _update_state(status='ok', last_check=now_ts)
        if not new_items:
            logger.info("[PlexPoller] No new watched media items.")
        else:
            logger.info(f"[PlexPoller] {len(new_items)} new watched media item(s).")
            max_ts = watermark
            for item in new_items:
                try:
                    itype = item.get('type')
                    if itype in (4, 'episode'):
                        status, msg = _process_episode(item)
                    elif itype in (1, 'movie'):
                        status, msg = _process_movie(item)
                    else:
                        continue

                    viewed_at = int(item.get('viewedAt', 0))
                    if viewed_at > max_ts:
                        max_ts = viewed_at

                    media_total += 1
                    _activity_log(status, msg, {
                        'title':    item.get('title'),
                        'viewedAt': viewed_at,
                        'type':     item.get('type'),
                    })
                    _update_state(episodes_session=media_total, last_episode=msg)
                except Exception as exc:
                    _activity_log('error', f"Failed processing watched item: {exc}")

            if max_ts > watermark:
                watermark = max_ts

            _save_persisted_state(watermark, media_total)

    return watermark, media_total


def _poller_loop():
    watermark, media_total = _load_persisted_state()
    _update_state(episodes_session=media_total)

    logger.info(f"[PlexPoller] Starting media poller loop.")

    while True:
        plex_url = (get_config('PLEX_URL') or '').rstrip('/')
        plex_token = get_config('PLEX_TOKEN') or ''
        poll_interval = int(get_config('PLEX_WATCH_INTERVAL', 3600))

        if not plex_url or not plex_token:
            _update_state(status='not_configured')
            time.sleep(10)
            continue

        with _poll_active:
            watermark, media_total = _execute_poll(watermark, media_total)

        # Calculate sleep until top-of-the-hour (or interval multiple)
        now_epoch = time.time()
        sleep_sec = poll_interval - int(now_epoch % poll_interval)
        if sleep_sec <= 0:
            sleep_sec = poll_interval

        next_dt = datetime.datetime.now() + datetime.timedelta(seconds=sleep_sec)
        next_ts = next_dt.strftime('%Y-%m-%d %H:%M:%S')
        _update_state(next_check=next_ts)

        logger.info(f"[PlexPoller] Next scheduled check at {next_ts} ({sleep_sec}s)")
        
        # Wait until top-of-hour OR until triggered manually
        _wake_event.wait(timeout=sleep_sec)
        _wake_event.clear()


def start():
    """Start the media poller as a background daemon thread."""
    t = threading.Thread(target=_poller_loop, name='plex-media-poller', daemon=True)
    t.start()
    logger.info("[PlexPoller] Background thread started.")
