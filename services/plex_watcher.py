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
from integrations.common import now_str as _now, get_plex_credentials

logger = logging.getLogger(__name__)

# ── Shared state (read by the Flask UI) ──────────────────────────────────────

watcher_state = {
    'enabled':               False,
    'dry_run':               True,
    'plex_url':              'Not configured',
    'status':                'starting',          # starting | ok | unreachable | not_configured
    'stream_count':          None,                # int or None
    'active_streams':        [],                  # list of detailed stream dicts
    'idle_streak':           0,                   # consecutive idle polls
    'idle_needed':           3,
    'poll_interval':         1200,
    'nzbget_active':         False,
    'nzbget_detail':         '',
    'plex_activity_active':  False,
    'plex_activity_detail':  '',
    'last_check':            None,                # ISO timestamp string
    'next_check':            None,                # ISO timestamp string
    'last_action':           None,                # description of last significant action
    'shutdown_fired':        False,
}

_state_lock = threading.Lock()
_last_fetch_time = 0
_log_callback = None


def set_log_callback(fn):
    global _log_callback
    _log_callback = fn


def _activity_log(status: str, message: str, payload: dict = None):
    if _log_callback:
        try:
            _log_callback(status, message, payload)
        except Exception as exc:
            logger.warning(f"[PlexWatcher] Log callback exception: {exc}")


def _update_state(**kwargs):
    with _state_lock:
        watcher_state.update(kwargs)


def _get_shutdown_mode() -> str:
    mode = get_config('PLEX_SHUTDOWN_MODE')
    if mode in ['disabled', 'dry_run', 'enabled']:
        return mode
    dry_run = get_config('PLEX_SHUTDOWN_DRY_RUN')
    if dry_run is False or str(dry_run).lower() == 'false':
        return 'enabled'
    return 'dry_run'


def get_state(refresh_live: bool = True):
    """
    Return a snapshot of the watcher state (thread-safe).
    Refreshes active stream info from Plex if cache is older than 5s.
    """
    global _last_fetch_time

    plex_url, plex_token = get_plex_credentials()
    shutdown_mode = _get_shutdown_mode()
    dry_run = (shutdown_mode == 'dry_run')
    enabled = bool(plex_url and plex_token and shutdown_mode != 'disabled')
    idle_needed = int(get_config('PLEX_IDLE_POLLS', 3))
    poll_interval = int(get_config('PLEX_POLL_INTERVAL', 1200))

    _update_state(
        enabled=enabled,
        shutdown_mode=shutdown_mode,
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
            has_dl, dl_detail = _has_active_downloads()
            has_act, act_detail = _has_active_plex_activities()
            if count is not None:
                _update_state(
                    status='ok',
                    stream_count=count,
                    active_streams=streams,
                    nzbget_active=has_dl,
                    nzbget_detail=dl_detail,
                    plex_activity_active=has_act,
                    plex_activity_detail=act_detail,
                    last_check=_now()
                )
            else:
                _update_state(
                    status='unreachable',
                    nzbget_active=has_dl,
                    nzbget_detail=dl_detail,
                    plex_activity_active=has_act,
                    plex_activity_detail=act_detail
                )
    with _state_lock:
        return dict(watcher_state)


def trigger_shutdown_now() -> dict:
    """Manually trigger host shutdown command immediately."""
    logger.info("[PlexWatcher] Manual host shutdown triggered by user.")
    _update_state(shutdown_fired=True)
    success, msg = _ssh_shutdown(force=True)
    if success:
        return {'status': 'success', 'message': msg}
    else:
        return {'status': 'error', 'message': msg}


# ── Plex polling ─────────────────────────────────────────────────────────────

def _get_active_sessions() -> tuple[int | None, list[dict]]:
    """
    Query the Plex /status/sessions endpoint for active stream count & metadata.
    Returns (count, streams_list).
    """
    plex_url, plex_token = get_plex_credentials()

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
        count = int(container.get('size', 0))
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


def _has_active_downloads() -> tuple[bool, str]:
    """Check if NZBGet is active (downloading or queued items)."""
    try:
        from integrations.nzbget import get_nzbget_status
        nzb_st = get_nzbget_status()
        if nzb_st.get('connected') and not nzb_st.get('download_paused', False):
            dls = nzb_st.get('downloads', [])
            rate_bps = nzb_st.get('download_rate_bps', 0)
            active_dls = [
                d for d in dls 
                if d.get('status') in ('DOWNLOADING', 'QUEUED', 'FETCHING', 'EXTRACTING', 'POST-PROCESSING')
            ]
            if rate_bps > 0 or len(active_dls) > 0:
                rate_str = nzb_st.get('download_rate', '')
                count = len(active_dls) or len(dls)
                detail = f"{count} NZBGet download(s) active" + (f" ({rate_str})" if rate_str else "")
                return True, detail
    except Exception as exc:
        logger.warning(f"[PlexWatcher] Failed checking NZBGet status: {exc}")
    return False, ""


def _has_active_plex_activities() -> tuple[bool, str]:
    """
    Query Plex /activities endpoint for running background jobs (library scans,
    thumbnail generation, intro/credit detection, database optimization).
    """
    plex_url, plex_token = get_plex_credentials()

    if not plex_url or not plex_token:
        return False, ""
    try:
        url = f"{plex_url}/activities"
        resp = requests.get(
            url,
            headers={'X-Plex-Token': plex_token, 'Accept': 'application/json'},
            params={'X-Plex-Token': plex_token},
            timeout=5,
        )
        if resp.status_code != 200:
            return False, ""
        data = resp.json()
        container = data.get('MediaContainer', {}) if isinstance(data, dict) else {}
        activities = container.get('Activity', []) or []
        
        if isinstance(activities, dict):
            activities = [activities]
            
        if isinstance(activities, list) and len(activities) > 0:
            act = activities[0]
            if isinstance(act, dict):
                title = act.get('title') or act.get('type') or 'Background Task'
                subtitle = act.get('subtitle') or ''
                detail = f"Plex {title}" + (f" ({subtitle})" if subtitle else "")
                return True, detail
    except Exception as exc:
        logger.debug(f"[PlexWatcher] Failed checking Plex background activities: {exc}")
    return False, ""


# ── SSH shutdown ─────────────────────────────────────────────────────────────

def _find_ssh_key(configured_path: str) -> str | None:
    if configured_path and os.path.exists(configured_path):
        return configured_path
    candidates = [
        '/root/.ssh/id_ed25519',
        '/root/.ssh/id_rsa',
        '/root/.ssh/id_ecdsa',
        '/root/.ssh/id_dsa',
    ]
    for c in candidates:
        if os.path.exists(c):
            logger.info(f"[PlexWatcher] Auto-detected SSH key at {c}")
            return c
    return None


def _ssh_shutdown(force: bool = False) -> tuple[bool, str]:
    """SSH into SSH_HOST and issue a Linux shutdown command."""
    ssh_host = get_config('SSH_HOST', '')
    ssh_port = int(get_config('SSH_PORT', 22))
    ssh_user = get_config('SSH_USER', '')
    ssh_password = get_config('SSH_PASSWORD', '')
    ssh_key_path = get_config('SSH_KEY_PATH', '/root/.ssh/id_rsa')
    shutdown_mode = _get_shutdown_mode()
    
    if shutdown_mode == 'disabled' and not force:
        msg = "Host shutdown watcher is disabled in configuration."
        logger.info(f"[PlexWatcher] {msg}")
        return False, msg

    dry_run = (shutdown_mode == 'dry_run')
    resolved_key_path = _find_ssh_key(ssh_key_path)

    if ssh_password:
        cmd = f'echo "{ssh_password}" | sudo -S docker stop -t 30 plex || true; echo "{ssh_password}" | sudo -S shutdown -h now || sudo poweroff'
    else:
        cmd = 'sudo docker stop -t 30 plex || true; sudo shutdown -h now || sudo poweroff'

    if dry_run and not force:
        msg = f"[DRY-RUN] Would SSH {ssh_user}@{ssh_host}:{ssh_port} and run: {cmd}"
        logger.warning(f"[PlexWatcher] {msg}")
        _update_state(last_action=f"[DRY-RUN] Shutdown simulated at {_now()}")
        _activity_log('warn', '[DRY-RUN] Idle shutdown threshold reached — simulated SSH shutdown')
        return True, msg

    if not ssh_host or not ssh_user:
        msg = f"Shutdown failed: SSH_HOST ('{ssh_host}') or SSH_USER ('{ssh_user}') missing"
        logger.error(f"[PlexWatcher] {msg}")
        _update_state(last_action=msg)
        _activity_log('error', msg)
        return False, msg

    try:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=ssh_host,
            port=ssh_port,
            username=ssh_user,
            password=ssh_password if ssh_password else None,
            key_filename=resolved_key_path if (resolved_key_path and not ssh_password) else resolved_key_path,
            timeout=15,
        )
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        client.close()

        if exit_status != 0:
            err_msg = f"SSH command failed (code {exit_status}): {err or out or 'Unknown error'}"
            logger.error(f"[PlexWatcher] {err_msg}")
            _update_state(last_action=f"Shutdown FAILED at {_now()}: {err_msg}")
            _activity_log('error', f"SSH shutdown command failed: {err_msg}")
            return False, err_msg

        msg = f"SSH shutdown command sent to {ssh_host}."
        logger.info(f"[PlexWatcher] {msg}")
        _update_state(last_action=f"Shutdown triggered at {_now()} — {msg}")
        _activity_log('warn', f"Host shutdown executed via SSH ({ssh_host})")
        return True, msg

    except Exception as exc:
        err_msg = f"SSH connection failed: {exc}"
        logger.error(f"[PlexWatcher] {err_msg}")
        _update_state(last_action=f"Shutdown FAILED at {_now()}: {err_msg}")
        _activity_log('error', f"Shutdown SSH connection error: {exc}")
        return False, err_msg


# ── Main loop ────────────────────────────────────────────────────────────────


def _watcher_loop():
    logger.info(f"[PlexWatcher] Starting power saver watcher loop. Delaying 15s to allow Plex to spin up...")
    time.sleep(15)
    idle_streak = 0

    while True:
        try:
            shutdown_mode = _get_shutdown_mode()
            if shutdown_mode == 'disabled':
                _update_state(
                    enabled=False,
                    shutdown_mode='disabled',
                    status='disabled',
                    stream_count=0,
                    active_streams=[],
                    idle_streak=0,
                    last_action='Power saver watcher disabled in configuration',
                    last_check=_now(),
                    next_check=None
                )
                time.sleep(10)
                continue

            plex_url, plex_token = get_plex_credentials()
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
            has_dl, dl_detail = _has_active_downloads()
            has_act, act_detail = _has_active_plex_activities()

            # Build base state shared by all branches
            base = dict(
                nzbget_active=has_dl,
                nzbget_detail=dl_detail,
                plex_activity_active=has_act,
                plex_activity_detail=act_detail,
                last_check=now_ts,
                next_check=next_ts,
            )

            if stream_count is None:
                logger.warning(f"[PlexWatcher] {now_ts} — Plex unreachable.")
                _update_state(**base, status='unreachable', stream_count=None, active_streams=[], idle_streak=idle_streak)
            elif stream_count > 0:
                logger.info(f"[PlexWatcher] {now_ts} — {stream_count} active stream(s). Idle streak reset.")
                idle_streak = 0
                _update_state(**base, status='ok', stream_count=stream_count, active_streams=active_streams,
                              idle_streak=0, last_action=f"{now_ts} — {stream_count} stream(s) active, idle streak reset")
            elif has_dl:
                logger.info(f"[PlexWatcher] {now_ts} — No Plex streams, but NZBGet downloads active ({dl_detail}). Idle streak reset.")
                idle_streak = 0
                _update_state(**base, status='ok', stream_count=0, active_streams=[],
                              idle_streak=0, last_action=f"{now_ts} — NZBGet active ({dl_detail}), idle streak reset")
            elif has_act:
                logger.info(f"[PlexWatcher] {now_ts} — No streams/downloads, but Plex task active ({act_detail}). Idle streak reset.")
                idle_streak = 0
                _update_state(**base, status='ok', stream_count=0, active_streams=[],
                              nzbget_active=False, nzbget_detail='',
                              idle_streak=0, last_action=f"{now_ts} — {act_detail}, idle streak reset")
            else:
                idle_streak += 1
                logger.info(f"[PlexWatcher] {now_ts} — System idle (no streams/downloads/tasks). Idle streak: {idle_streak}/{idle_needed}")
                _update_state(**base, status='ok', stream_count=0, active_streams=[],
                              nzbget_active=False, nzbget_detail='',
                              plex_activity_active=False, plex_activity_detail='',
                              idle_streak=idle_streak,
                              last_action=f"{now_ts} — System idle (idle streak {idle_streak}/{idle_needed})")

                if idle_streak >= idle_needed:
                    logger.warning(f"[PlexWatcher] Idle threshold reached ({idle_streak} polls). Triggering shutdown.")
                    _update_state(shutdown_fired=True)
                    _ssh_shutdown()
                    idle_streak = 0
                    _update_state(idle_streak=0)

            time.sleep(poll_interval)
        except Exception as exc:
            logger.error(f"[PlexWatcher] Unhandled loop exception: {exc}")
            time.sleep(10)


def start():
    """Start the Plex watcher as a background daemon thread."""
    t = threading.Thread(target=_watcher_loop, name='plex-watcher', daemon=True)
    t.start()
    logger.info("[PlexWatcher] Background thread started.")
