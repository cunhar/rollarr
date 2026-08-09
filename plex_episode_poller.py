"""
plex_episode_poller.py
----------------------
Background daemon thread that polls Plex watch history every PLEX_WATCH_INTERVAL
seconds (default: 3600 = 1 hour). For each newly-watched TV episode found, it
resolves the series in Sonarr and applies the rolling-window monitoring logic
(monitor + search the next N episodes).

Plex API endpoints used
-----------------------
GET /status/sessions/history/all?type=4&sort=viewedAt:asc
    Episode watch history. type=4 = TV episodes only.

GET /library/metadata/{ratingKey}
    Full item metadata (fallback if top-level history fields are missing).

State persistence
-----------------
A high-water mark (last processed viewedAt Unix timestamp) is saved to
CONFIG_DIR/plex_poll_state.json so episodes aren't re-processed after a restart.

Environment variables
---------------------
PLEX_URL             Base Plex URL (e.g. http://plex:32400)
PLEX_TOKEN           Plex authentication token
PLEX_WATCH_INTERVAL  Seconds between polls (default: 3600)
"""

import os
import json
import time
import threading
import logging
import datetime

import requests

from sonarr_api import (
    ROLLING_WINDOW,
    find_series_id_by_title,
    get_episodes,
    monitor_episode,
    search_episode,
)

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

PLEX_URL      = os.environ.get('PLEX_URL', '').rstrip('/')
PLEX_TOKEN    = os.environ.get('PLEX_TOKEN', '')
POLL_INTERVAL = int(os.environ.get('PLEX_WATCH_INTERVAL', 3600))

CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

STATE_FILE = os.path.join(CONFIG_DIR, 'plex_poll_state.json')

# ── Shared state (read by the Flask UI) ──────────────────────────────────────

poller_state = {
    'enabled':           bool(PLEX_URL and PLEX_TOKEN),
    'poll_interval':     POLL_INTERVAL,
    'status':            'starting',   # starting | ok | unreachable | not_configured
    'last_check':        None,
    'next_check':        None,
    'episodes_session':  0,            # episodes processed since container start
    'last_episode':      None,         # human-readable last processed episode
    'last_error':        None,
}

_state_lock = threading.Lock()

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
    with _state_lock:
        return dict(poller_state)


# ── High-water mark persistence ───────────────────────────────────────────────

def _load_watermark() -> int:
    """Return the last saved viewedAt timestamp (Unix seconds), or 0."""
    try:
        with open(STATE_FILE, 'r') as f:
            return int(json.load(f).get('last_viewed_at', 0))
    except Exception:
        return 0


def _save_watermark(ts: int):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({'last_viewed_at': ts}, f)
    except Exception as e:
        logger.warning(f"[PlexPoller] Could not save watermark: {e}")


# ── Plex API helpers ──────────────────────────────────────────────────────────

def _plex_get(path: str, params: dict = None) -> dict | None:
    if not PLEX_URL or not PLEX_TOKEN:
        return None
    try:
        r = requests.get(
            f"{PLEX_URL}{path}",
            headers={'Accept': 'application/json'},
            params={'X-Plex-Token': PLEX_TOKEN, **(params or {})},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning(f"[PlexPoller] Plex request failed ({path}): {exc}")
        return None


def _fetch_new_watched(since_ts: int) -> list | None:
    """
    Return episode history items with viewedAt > since_ts, sorted oldest-first.
    Returns None if Plex was unreachable.
    """
    data = _plex_get('/status/sessions/history/all', {
        'type': 4,                        # TV episodes only
        'sort': 'viewedAt:asc',
        'X-Plex-Container-Start': 0,
        'X-Plex-Container-Size': 200,
    })
    if data is None:
        return None
    items = data.get('MediaContainer', {}).get('Metadata') or []
    return [i for i in items if int(i.get('viewedAt', 0)) > since_ts]


def _enrich_metadata(item: dict) -> dict:
    """
    Fill in missing grandparentTitle / parentIndex / index from the full
    metadata endpoint when the history record is incomplete.
    """
    if item.get('grandparentTitle') and item.get('parentIndex') is not None and item.get('index') is not None:
        return item
    meta_data = _plex_get(f"/library/metadata/{item['ratingKey']}")
    if meta_data:
        hit = (meta_data.get('MediaContainer', {}).get('Metadata') or [None])[0]
        if hit:
            item = {**item, **{k: hit[k] for k in ('grandparentTitle', 'parentIndex', 'index') if k in hit}}
    return item


# ── Sonarr rolling-window logic ───────────────────────────────────────────────

def _process_episode(item: dict) -> tuple[str, str]:
    """
    Given a Plex history item for a TV episode, apply the Sonarr rolling window.
    Returns (status, message).
    """
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

    next_eps = regular[current_idx + 1 : current_idx + 1 + ROLLING_WINDOW]

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


# ── Main loop ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _poller_loop():
    if not PLEX_URL or not PLEX_TOKEN:
        logger.warning("[PlexPoller] PLEX_URL or PLEX_TOKEN not set — episode poller disabled.")
        _update_state(status='not_configured')
        return

    logger.info(f"[PlexPoller] Starting. interval={POLL_INTERVAL}s")
    watermark       = _load_watermark()
    episodes_total  = 0

    while True:
        now_ts  = _now()
        next_ts = (datetime.datetime.now() + datetime.timedelta(seconds=POLL_INTERVAL)).strftime('%Y-%m-%d %H:%M:%S')

        logger.info(f"[PlexPoller] Checking Plex history (since viewedAt={watermark})")
        new_items = _fetch_new_watched(watermark)

        if new_items is None:
            logger.warning("[PlexPoller] Plex unreachable.")
            _update_state(status='unreachable', last_check=now_ts, next_check=next_ts)
        else:
            _update_state(status='ok', last_check=now_ts, next_check=next_ts)
            if not new_items:
                logger.info("[PlexPoller] No new watched episodes.")
            else:
                logger.info(f"[PlexPoller] {len(new_items)} new watched episode(s).")
                max_ts = watermark
                for item in new_items:
                    try:
                        status, msg = _process_episode(item)
                        viewed_at   = int(item.get('viewedAt', 0))
                        if viewed_at > max_ts:
                            max_ts = viewed_at
                        episodes_total += 1
                        _activity_log(status, msg, {
                            'title':     item.get('title'),
                            'viewedAt':  viewed_at,
                            'show':      item.get('grandparentTitle'),
                        })
                        _update_state(episodes_session=episodes_total, last_episode=msg)
                    except Exception as exc:
                        _activity_log('error', f"Failed processing episode: {exc}")

                if max_ts > watermark:
                    watermark = max_ts
                    _save_watermark(watermark)

        time.sleep(POLL_INTERVAL)


def start():
    """Start the episode poller as a background daemon thread."""
    t = threading.Thread(target=_poller_loop, name='plex-episode-poller', daemon=True)
    t.start()
    logger.info("[PlexPoller] Background thread started.")
