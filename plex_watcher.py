"""
plex_watcher.py
---------------
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

import os
import time
import threading
import logging
import datetime
import xml.etree.ElementTree as ET

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
    'idle_streak':    0,                   # consecutive idle polls
    'idle_needed':    IDLE_POLLS_NEEDED,
    'poll_interval':  POLL_INTERVAL,
    'last_check':     None,                # ISO timestamp string
    'next_check':     None,                # ISO timestamp string
    'last_action':    None,                # description of last significant action
    'shutdown_fired': False,
}

_state_lock = threading.Lock()


def _update_state(**kwargs):
    with _state_lock:
        watcher_state.update(kwargs)


def get_state():
    """Return a snapshot of the watcher state (thread-safe)."""
    with _state_lock:
        return dict(watcher_state)


# ── Plex polling ─────────────────────────────────────────────────────────────

def _get_active_streams() -> int | None:
    """
    Query the Plex /status/sessions endpoint.
    Returns the number of active streams, or None on error.
    """
    if not PLEX_URL or not PLEX_TOKEN:
        return None
    try:
        url = f"{PLEX_URL}/status/sessions"
        resp = requests.get(
            url,
            headers={'X-Plex-Token': PLEX_TOKEN, 'Accept': 'application/xml'},
            params={'X-Plex-Token': PLEX_TOKEN},
            timeout=10,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        count = int(root.attrib.get('size', 0))
        return count
    except Exception as exc:
        logger.warning(f"[PlexWatcher] Failed to query Plex sessions: {exc}")
        return None


# ── SSH shutdown ─────────────────────────────────────────────────────────────

def _ssh_shutdown():
    """
    SSH into SSH_HOST and issue a Linux shutdown command.
    Uses paramiko for pure-Python SSH — no binary required.
    """
    cmd = 'sudo shutdown -h +1'
    if DRY_RUN:
        logger.warning(
            f"[PlexWatcher] DRY-RUN: would SSH {SSH_USER}@{SSH_HOST}:{SSH_PORT} "
            f"and run: {cmd}"
        )
        _update_state(last_action=f"[DRY-RUN] Shutdown would have been triggered at "
                                   f"{_now()}")
        return

    if not SSH_HOST or not SSH_USER:
        logger.error("[PlexWatcher] SSH_HOST or SSH_USER not configured — cannot shut down.")
        _update_state(last_action="Shutdown skipped: SSH_HOST or SSH_USER missing")
        return

    try:
        import paramiko  # imported lazily so startup isn't affected if not installed

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

        stream_count = _get_active_streams()

        if stream_count is None:
            # Plex unreachable — don't change idle streak, just mark status
            logger.warning(f"[PlexWatcher] {now_ts} — Plex unreachable.")
            _update_state(
                status='unreachable',
                stream_count=None,
                last_check=now_ts,
                next_check=next_ts,
                idle_streak=idle_streak,
            )
        elif stream_count > 0:
            # Active streams — reset idle counter
            logger.info(
                f"[PlexWatcher] {now_ts} — {stream_count} active stream(s). "
                f"Idle streak reset."
            )
            idle_streak = 0
            _update_state(
                status='ok',
                stream_count=stream_count,
                last_check=now_ts,
                next_check=next_ts,
                idle_streak=0,
                last_action=f"{now_ts} — {stream_count} stream(s) active, idle streak reset",
            )
        else:
            # Zero streams
            idle_streak += 1
            logger.info(
                f"[PlexWatcher] {now_ts} — No active streams. "
                f"Idle streak: {idle_streak}/{IDLE_POLLS_NEEDED}"
            )
            _update_state(
                status='ok',
                stream_count=0,
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
                # Reset streak so we don't hammer shutdown on every subsequent poll
                idle_streak = 0
                _update_state(idle_streak=0)

        time.sleep(POLL_INTERVAL)


def start():
    """Start the Plex watcher as a background daemon thread."""
    t = threading.Thread(target=_watcher_loop, name='plex-watcher', daemon=True)
    t.start()
    logger.info("[PlexWatcher] Background thread started.")
