"""
services/plex_watcher.py
------------------------
Background daemon thread that periodically polls Plex for active streams.
If no streams are detected for PLEX_IDLE_POLLS consecutive checks (each
separated by PLEX_POLL_INTERVAL seconds), the host machine is shut down
via SSH.
"""
from __future__ import annotations

import os
import time
import threading
import logging
import datetime

import requests
from config_store import get_config

logger = logging.getLogger(__name__)

# ── Shared state (read by the Flask UI) ──────────────────────────────────────

watcher_state = {
    'enabled':        False,
    'dry_run':        True,
    'plex_url':       'Not configured',
    'status':         'starting',          # starting | ok | unreachable | not_configured
    'stream_count':   None,                # int or None
    'active_streams': [],                  # list of detailed stream dicts
    'idle_streak':    0,                   # consecutive idle polls
    'idle_needed':    3,
    'poll_interval':  1200,
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

    plex_url = (get_config('PLEX_URL') or '').rstrip('/')
    plex_token = get_config('PLEX_TOKEN') or ''
    dry_run = bool(get_config('PLEX_SHUTDOWN_DRY_RUN', True))
    idle_needed = int(get_config('PLEX_IDLE_POLLS', 3))
    poll_interval = int(get_config('PLEX_POLL_INTERVAL', 1200))

    _update_state(
        enabled=bool(plex_url and plex_token),
        dry_run=dry_run,
        plex_url=plex_url or 'Not configured',
        idle_needed=idle_needed,
        poll_interval=poll_interval,
    )

    if refresh_live and plex_url and plex_token:
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


def trigger_shutdown_now() -> dict:
    """Manually trigger host shutdown command immediately."""
    logger.info("[PlexWatcher] Manual host shutdown triggered by user.")
    _update_state(shutdown_fired=True)
    dry_run = bool(get_config('PLEX_SHUTDOWN_DRY_RUN', True))
    _ssh_shutdown()
    if dry_run:
        return {'status': 'success', 'message': '[DRY-RUN] Shutdown command simulated'}
    return {'status': 'success', 'message': 'Shutdown command sent to host'}


# ── Plex polling ─────────────────────────────────────────────────────────────

def _get_active_sessions() -> tuple[int | None, list[dict]]:
    """
    Query the Plex /status/sessions endpoint for active stream count & metadata.
    Returns (count, streams_list).
    """
    plex_url = (get_config('PLEX_URL') or '').rstrip('/')
    plex_token = get_config('PLEX_TOKEN') or ''

    if not plex_url or not plex_token:
        return None, []
    try:
        url = f"{plex_url}/status/sessions"
        resp = requests.get(
            url,
            headers={'X-Plex-Token': plex_token, 'Accept': 'application/json'},
            params={'X-Plex-Token': plex_token},
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
            if not transcode_data or v_dec in ('directplay', ''):
                decision = 'DIRECT PLAY'
            elif v_dec == 'copy':
                decision = 'DIRECT STREAM'
            elif v_dec == 'transcode':
                decision = 'TRANSCODE'
            else:
                decision = 'DIRECT PLAY'
                
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
    dry_run = bool(get_config('PLEX_SHUTDOWN_DRY_RUN', True))
    ssh_host = get_config('SSH_HOST', '')
    ssh_port = int(get_config('SSH_PORT', 22))
    ssh_user = get_config('SSH_USER', '')
    ssh_key_path = get_config('SSH_KEY_PATH', '/root/.ssh/id_rsa')

    if dry_run:
        logger.warning(
            f"[PlexWatcher] DRY-RUN: would SSH {ssh_user}@{ssh_host}:{ssh_port} "
            f"and run: {cmd}"
        )
        _update_state(last_action=f"[DRY-RUN] Shutdown would have been triggered at {_now()}")
        return

    if not ssh_host or not ssh_user:
        logger.error("[PlexWatcher] SSH_HOST or SSH_USER not configured — cannot shut down.")
        _update_state(last_action="Shutdown skipped: SSH_HOST or SSH_USER missing")
        return

    try:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=ssh_host,
            port=ssh_port,
            username=ssh_user,
            key_filename=ssh_key_path if os.path.exists(ssh_key_path) else None,
            timeout=15,
        )
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        client.close()

        msg = (
            f"SSH shutdown sent to {ssh_host}. "
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
    idle_streak = 0

    while True:
        plex_url = (get_config('PLEX_URL') or '').rstrip('/')
        plex_token = get_config('PLEX_TOKEN') or ''
        poll_interval = int(get_config('PLEX_POLL_INTERVAL', 1200))
        idle_needed = int(get_config('PLEX_IDLE_POLLS', 3))

        if not plex_url or not plex_token:
            _update_state(status='not_configured')
            time.sleep(10)
            continue

        now_ts = _now()
        next_ts = (
            datetime.datetime.now() + datetime.timedelta(seconds=poll_interval)
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
                f"Idle streak: {idle_streak}/{idle_needed}"
            )
            _update_state(
                status='ok',
                stream_count=0,
                active_streams=[],
                last_check=now_ts,
                next_check=next_ts,
                idle_streak=idle_streak,
                last_action=(
                    f"{now_ts} — No streams (idle streak {idle_streak}/{idle_needed})"
                ),
            )

            if idle_streak >= idle_needed:
                logger.warning(
                    f"[PlexWatcher] Idle threshold reached ({idle_streak} polls). "
                    f"Triggering shutdown."
                )
                _update_state(shutdown_fired=True)
                _ssh_shutdown()
                idle_streak = 0
                _update_state(idle_streak=0)

        time.sleep(poll_interval)


def start():
    """Start the Plex watcher as a background daemon thread."""
    t = threading.Thread(target=_watcher_loop, name='plex-watcher', daemon=True)
    t.start()
    logger.info("[PlexWatcher] Background thread started.")
