from flask import Flask, request, jsonify, render_template

import requests
import os
import logging
from logging.handlers import RotatingFileHandler
import json
from collections import deque
import threading
import concurrent.futures

import config_store
from config_store import mask_secret
from integrations.common import now_str
from integrations.sonarr import (
    get_sonarr_url,
    get_sonarr_api_key,
    get_rolling_window,
    get_sonarr_headers,
)
from integrations.radarr import (
    get_radarr_url,
    get_radarr_api_key,
    get_radarr_headers,
)
from integrations.nzbget import (
    get_nzbget_status,
    get_nzbget_url,
    get_nzbget_username,
    get_nzbget_password,
)
from services import plex_watcher, plex_poller
from services.disk_service import get_disk_space_summary

# Persistent configuration directories
CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

HISTORY_FILE = os.path.join(CONFIG_DIR, 'history.json')
LOG_FILE     = os.path.join(CONFIG_DIR, 'rolarr.log')

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

try:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"Could not initialize file logging: {e}")

app = Flask(__name__)

# In-memory activity log (last 20 entries) with thread lock
webhook_history = deque(maxlen=20)
history_lock = threading.Lock()


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                with history_lock:
                    webhook_history.clear()
                    for item in data:
                        webhook_history.append(item)
            logger.info(f"Loaded {len(data)} activity log entries from {HISTORY_FILE}")
        except Exception as e:
            logger.warning(f"Could not load activity log file: {e}")


def save_history():
    try:
        with history_lock:
            data = list(webhook_history)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save activity log file: {e}")


def log_call(status, message, payload=None):
    entry = {
        "timestamp": now_str(),
        "status": status,
        "message": message,
        "payload": payload,
    }
    with history_lock:
        webhook_history.appendleft(entry)
    save_history()


load_history()
plex_poller.set_log_callback(log_call)
plex_watcher.set_log_callback(log_call)
plex_poller.start()
plex_watcher.start()


# ── Live Service Connection Checks ───────────────────────────────────────────

def _check_sonarr_status() -> dict:
    url, key = get_sonarr_url(), get_sonarr_api_key()
    if not url or not key:
        return {'status': 'Disconnected', 'ok': False}
    try:
        res = requests.get(f"{url}/api/v3/system/status", headers=get_sonarr_headers(), timeout=2)
        status = "Connected" if res.status_code == 200 else f"Error ({res.status_code})"
    except Exception:
        status = "Unreachable"
    return {'status': status, 'ok': status == 'Connected'}


def _check_radarr_status() -> dict:
    url, key = get_radarr_url(), get_radarr_api_key()
    if not url or not key:
        return {'status': 'Disconnected', 'ok': False}
    try:
        res = requests.get(f"{url}/api/v3/system/status", headers=get_radarr_headers(), timeout=2)
        status = "Connected" if res.status_code == 200 else f"Error ({res.status_code})"
    except Exception:
        status = "Unreachable"
    return {'status': status, 'ok': status == 'Connected'}


def _check_plex_status() -> dict:
    cfg = config_store.get_all_config()
    plex_url = (cfg.get('PLEX_URL') or '').rstrip('/')
    plex_token = cfg.get('PLEX_TOKEN') or ''
    if not plex_url or not plex_token:
        return {'status': 'Disconnected', 'ok': False}
    try:
        res = requests.get(
            f"{plex_url}/identity",
            headers={'X-Plex-Token': plex_token, 'Accept': 'application/json'},
            timeout=2,
        )
        status = "Connected" if res.status_code == 200 else f"Error ({res.status_code})"
    except Exception:
        status = "Unreachable"
    return {'status': status, 'ok': status == 'Connected'}


def get_service_connections() -> dict:
    """Run connection checks concurrently to reduce dashboard latency."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        f_sonarr = executor.submit(_check_sonarr_status)
        f_radarr = executor.submit(_check_radarr_status)
        f_plex = executor.submit(_check_plex_status)
        return {
            'sonarr': f_sonarr.result(),
            'radarr': f_radarr.result(),
            'plex':   f_plex.result(),
        }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    cfg = config_store.get_all_config()
    conns = get_service_connections()

    with history_lock:
        history_list = list(webhook_history)

    return render_template(
        "index.html",
        cfg=cfg,
        sonarr_url=get_sonarr_url() or "Not Configured",
        masked_key=mask_secret(get_sonarr_api_key()),
        status_text=conns['sonarr']['status'],
        radarr_url=get_radarr_url() or "Not Configured",
        radarr_masked_key=mask_secret(get_radarr_api_key()),
        radarr_status_text=conns['radarr']['status'],
        plex_conn_text=conns['plex']['status'],
        nzbget_url=get_nzbget_url() or "Not Configured",
        nzbget_username=get_nzbget_username() or "Not Configured",
        nzbget_masked_pass=mask_secret(get_nzbget_password()),
        nzbget_status=get_nzbget_status(),
        history=history_list,
        rolling_window=get_rolling_window(),
        plex_status=plex_watcher.get_state(),
        poller_status=plex_poller.get_state(),
        disk_info=get_disk_space_summary(),
    )


@app.route('/api/config/save', methods=['POST'])
def api_config_save():
    try:
        data = request.json or {}
        updated = config_store.save_config(data)
        logger.info("[App] Saved updated encrypted configuration via UI.")
        log_call('ok', 'Configuration saved securely via web dashboard')
        return jsonify({'status': 'success', 'message': 'Configuration saved securely', 'config': updated}), 200
    except Exception as exc:
        logger.error(f"[App] Failed to save configuration: {exc}")
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/connection-status')
def api_connection_status():
    return jsonify(get_service_connections())


@app.route('/api/plex-status')
def api_plex_status():
    return jsonify(plex_watcher.get_state())


@app.route('/api/poller-status')
def api_poller_status():
    return jsonify(plex_poller.get_state())


@app.route('/api/poller-run', methods=['POST'])
def api_poller_run():
    res = plex_poller.trigger_now()
    return jsonify(res), 200 if res['status'] == 'success' else 400


@app.route('/api/shutdown-now', methods=['POST'])
def api_shutdown_now():
    res = plex_watcher.trigger_shutdown_now()
    return jsonify(res), 200 if res['status'] == 'success' else 400


@app.route('/api/nzbget-status')
def api_nzbget_status():
    return jsonify(get_nzbget_status())


@app.route('/api/clear-logs', methods=['POST'])
def api_clear_logs():
    with history_lock:
        webhook_history.clear()
    save_history()
    logger.info("Activity history cleared via UI.")
    return jsonify({"status": "success"}), 200


@app.route('/api/activity')
def api_activity():
    """Return the current activity log as JSON for live polling."""
    with history_lock:
        return jsonify(list(webhook_history)), 200


@app.route('/api/disk-space')
def api_disk_space():
    """Return current disk space for downloads, tv, movies, and arr root folders."""
    return jsonify(get_disk_space_summary()), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
