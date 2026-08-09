"""
services/plex_watcher.py
------------------------
Background daemon thread that periodically polls Plex for active streams.
If no streams are detected for PLEX_IDLE_POLLS consecutive checks (each
separated by PLEX_POLL_INTERVAL seconds), the host machine is shut down
via SSH.

Environment variables
---------------------
PLEX_URL               Base Plex URL (e.g. http://localhost:32400)
PLEX_TOKEN             Plex authentication token (X-Plex-Token)
PLEX_POLL_INTERVAL     Seconds between polls  (default: 1200 = 20 min)
PLEX_IDLE_POLLS        Consecutive idle polls before shutdown (default: 3)
SSH_HOST               Hostname / IP of the host to SSH into
SSH_PORT               SSH port (default: 22)
SSH_USER               SSH username
SSH_KEY_PATH           Path to SSH private key inside the container
                       (default: /root/.ssh/id_rsa)
PLEX_SHUTDOWN_DRY_RUN  If 'true', log only — never actually shut down
"""
from __future__ import annotations

import os
import time
import threading
import logging
import datetime

import requests

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

PLEX_URL          = os.environ.get('PLEX_URL', '').rstrip('/')
PLEX_TOKEN        = os.environ.get('PLEX_TOKEN', '')
POLL_INTERVAL     = int(os.environ.get('PLEX_POLL_INTERVAL', 1200))   # seconds
IDLE_POLLS_NEEDED = int(os.environ.get('PLEX_IDLE_POLLS', 3))

SSH_HOST     = os.environ.get('SSH_HOST', '')
SSH_PORT     = int(os.environ.get('SSH_PORT', 22))
SSH_USER     = os.environ.get('SSH_USER', '')
SSH_KEY_PATH = os.environ.get('SSH_KEY_PATH', '/root/.ssh/id_rsa')

DRY_RUN = os.environ.get('PLEX_SHUTDOWN_DRY_RUN', 'false').lower() in ('true', '1', 'yes')

# ── Shared state (read by the Flask UI) ──────────────────────────────────────

watcher_state = {
    'enabled':        bool(PLEX_URL and PLEX_TOKEN),
    'dry_run':        DRY_RUN,
    'plex_url':       PLEX_URL or 'Not configured',
    'status':         'starting',          # starting | ok | unreachable | not_configured
    'stream_count':   None,                # int or None
    'active_streams': [],                  # list of detailed stream dicts
    'idle_streak':    0,                   # consecutive idle polls
    'idle_needed':    IDLE_POLLS_NEEDED,
    'poll_interval':  POLL_INTERVAL,
    'last_check':     None,                # ISO timestamp string
    'next_check':     None,                # ISO timestamp string
    'last_action':    None,                # description of last significant action
    'shutdown_fired': False,
}

_state_lock = threading.Lock()
_last_fetch_time = 0


def _update_state(**kwargs):
    with _state_lock:
        watcher_state.update(kwargs)


def get_state(refresh_live: bool = True):
    """
    Return a snapshot of the watcher state (thread-safe).
    Refreshes active stream info from Plex if cache is older than 5s.
    """
    global _last_fetch_time
    if refresh_live and PLEX_URL and PLEX_TOKEN:
        now_time = time.time()
        if now_time - _last_fetch_time > 5:
            _last_fetch_time = now_time
            count, streams = _get_active_sessions()
            if count is not None:
                _update_state(status='ok', stream_count=count, active_streams=streams, last_check=_now())
            else:
                _update_state(status='unreachable')
    with _state_lock:
        return dict(watcher_state)


# ── Plex polling ─────────────────────────────────────────────────────────────

def _get_active_sessions() -> tuple[int | None, list[dict]]:
    """
    Query the Plex /status/sessions endpoint for active stream count & metadata.
    Returns (count, streams_list).
    """
    if not PLEX_URL or not PLEX_TOKEN:
        return None, []
    try:
        url = f"{PLEX_URL}/status/sessions"
        resp = requests.get(
            url,
            headers={'X-Plex-Token': PLEX_TOKEN, 'Accept': 'application/json'},
            params={'X-Plex-Token': PLEX_TOKEN},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        container = data.get('MediaContainer', {})
        count = container.get('size', 0)
        items = container.get('Metadata', []) or []
        
        streams = []
        for item in items:
            user_data = item.get('User') or {}
            player_data = item.get('Player') or {}
            session_data = item.get('Session') or {}
            transcode_data = item.get('TranscodeSession') or {}
            
            user_name = user_data.get('title') or 'Unknown'
            user_thumb = user_data.get('thumb') or ''
            
            p_title = player_data.get('title') or ''
            p_device = player_data.get('device') or ''
            player_state = player_data.get('state', 'playing')
            device_str = f"{p_title} ({p_device})" if p_device and p_device != p_title else p_title or p_device or 'Unknown Device'
            
            itype = item.get('type')
            if itype == 'episode':
                show = item.get('grandparentTitle', '')
                s_num = item.get('parentIndex')
                e_num = item.get('index')
                ep_title = item.get('title', '')
                if s_num is not None and e_num is not None:
                    title_str = f"{show} S{int(s_num):02d}E{int(e_num):02d}"
                    if ep_title:
                        title_str += f" - {ep_title}"
                else:
                    title_str = show or ep_title or 'TV Episode'
            else:
                movie_title = item.get('title', '')
                year = item.get('year')
                title_str = f"{movie_title} ({year})" if year else movie_title or 'Movie'
                
            view_offset = int(item.get('viewOffset', 0))
            duration = int(item.get('duration', 0))
            pct = round((view_offset / duration * 100), 1) if duration > 0 else 0
            rem_ms = max(0, duration - view_offset)
            rem_mins = round(rem_ms / 60000)
            
            v_dec = (transcode_data.get('videoDecision') or '').lower()
            if v_dec == 'directplay' or not transcode_data:
                decision = 'DIRECT PLAY'
            elif v_dec == 'copy':
                decision = 'DIRECT STREAM'
            else:
                decision = 'TRANSCODE'
                
            is_local = player_data.get('local', True)
            loc = 'LAN' if is_local or session_data.get('location') == 'lan' else 'WAN'
            
            bw_kbps = int(session_data.get('bandwidth', 0) or item.get('bandwidth', 0))
            if bw_kbps >= 1000:
                bw_str = f"{bw_kbps / 1000:.1f} Mbps"
            elif bw_kbps > 0:
                bw_str = f"{bw_kbps} Kbps"
            else:
                bw_str = "—"
                
            streams.append({
                'user': user_name,
                'user_thumb': user_thumb,
                'device': device_str,
                'state': player_state,
                'title': title_str,
                'progress_pct': pct,
                'remaining_mins': rem_mins,
                'decision': decision,
                'location': loc,
                'bandwidth': bw_str
            })
            
        return count, streams
    except Exception as exc:
        logger.warning(f"[PlexWatcher] Failed to query Plex sessions: {exc}")
        return None, []


# ── SSH shutdown ─────────────────────────────────────────────────────────────

def _ssh_shutdown():
    """SSH into SSH_HOST and issue a Linux shutdown command."""
    cmd = 'sudo shutdown -h +1'
    if DRY_RUN:
        logger.warning(
            f"[PlexWatcher] DRY-RUN: would SSH {SSH_USER}@{SSH_HOST}:{SSH_PORT} "
            f"and run: {cmd}"
        )
        _update_state(last_action=f"[DRY-RUN] Shutdown would have been triggered at {_now()}")
        return

    if not SSH_HOST or not SSH_USER:
        logger.error("[PlexWatcher] SSH_HOST or SSH_USER not configured — cannot shut down.")
        _update_state(last_action="Shutdown skipped: SSH_HOST or SSH_USER missing")
        return

    try:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            key_filename=SSH_KEY_PATH if os.path.exists(SSH_KEY_PATH) else None,
            timeout=15,
        )
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        client.close()

        msg = (
            f"SSH shutdown sent to {SSH_HOST}. "
            f"Exit={exit_status}. stdout={out!r} stderr={err!r}"
        )
        logger.info(f"[PlexWatcher] {msg}")
        _update_state(last_action=f"Shutdown triggered at {_now()} — {msg}")

    except Exception as exc:
        logger.error(f"[PlexWatcher] SSH shutdown failed: {exc}")
        _update_state(last_action=f"Shutdown FAILED at {_now()}: {exc}")


# ── Main loop ────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _watcher_loop():
    if not PLEX_URL or not PLEX_TOKEN:
        logger.warning("[PlexWatcher] PLEX_URL or PLEX_TOKEN not set — watcher disabled.")
        _update_state(status='not_configured')
        return

    logger.info(
        f"[PlexWatcher] Starting. poll_interval={POLL_INTERVAL}s  "
        f"idle_polls_needed={IDLE_POLLS_NEEDED}  dry_run={DRY_RUN}"
    )

    idle_streak = 0

    while True:
        now_ts = _now()
        next_ts = (
            datetime.datetime.now() + datetime.timedelta(seconds=POLL_INTERVAL)
        ).strftime('%Y-%m-%d %H:%M:%S')

        stream_count, active_streams = _get_active_sessions()

        if stream_count is None:
            logger.warning(f"[PlexWatcher] {now_ts} — Plex unreachable.")
            _update_state(
                status='unreachable',
                stream_count=None,
                active_streams=[],
                last_check=now_ts,
                next_check=next_ts,
                idle_streak=idle_streak,
            )
        elif stream_count > 0:
            logger.info(
                f"[PlexWatcher] {now_ts} — {stream_count} active stream(s). "
                f"Idle streak reset."
            )
            idle_streak = 0
            _update_state(
                status='ok',
                stream_count=stream_count,
                active_streams=active_streams,
                last_check=now_ts,
                next_check=next_ts,
                idle_streak=0,
                last_action=f"{now_ts} — {stream_count} stream(s) active, idle streak reset",
            )
        else:
            idle_streak += 1
            logger.info(
                f"[PlexWatcher] {now_ts} — No active streams. "
                f"Idle streak: {idle_streak}/{IDLE_POLLS_NEEDED}"
            )
            _update_state(
                status='ok',
                stream_count=0,
                active_streams=[],
                last_check=now_ts,
                next_check=next_ts,
                idle_streak=idle_streak,
                last_action=(
                    f"{now_ts} — No streams (idle streak {idle_streak}/{IDLE_POLLS_NEEDED})"
                ),
            )

            if idle_streak >= IDLE_POLLS_NEEDED:
                logger.warning(
                    f"[PlexWatcher] Idle threshold reached ({idle_streak} polls). "
                    f"Triggering shutdown."
                )
                _update_state(shutdown_fired=True)
                _ssh_shutdown()
                idle_streak = 0
                _update_state(idle_streak=0)

        time.sleep(POLL_INTERVAL)


def start():
    """Start the Plex watcher as a background daemon thread."""
    t = threading.Thread(target=_watcher_loop, name='plex-watcher', daemon=True)
    t.start()
    logger.info("[PlexWatcher] Background thread started.")
