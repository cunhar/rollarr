from flask import Flask, request, jsonify, render_template
import requests
import os
import logging
from logging.handlers import RotatingFileHandler
import json
from collections import deque
import datetime
import threading

import config_store
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
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    entry = {
        "timestamp": ts,
        "status": status,
        "message": message,
        "payload": payload,
    }
    with history_lock:
        webhook_history.appendleft(entry)
    save_history()


load_history()
plex_poller.set_log_callback(log_call)
plex_poller.start()
plex_watcher.start()


# ── Live Service Connection Checks ───────────────────────────────────────────

def get_service_connections() -> dict:
    cfg = config_store.get_all_config()

    sonarr_url = get_sonarr_url()
    sonarr_api_key = get_sonarr_api_key()
    radarr_url = get_radarr_url()
    radarr_api_key = get_radarr_api_key()
    plex_url = (cfg.get('PLEX_URL') or '').rstrip('/')
    plex_token = cfg.get('PLEX_TOKEN') or ''

    # Sonarr check
    sonarr_status = "Disconnected"
    if sonarr_url and sonarr_api_key:
        try:
            res = requests.get(
                f"{sonarr_url}/api/v3/system/status",
                headers=get_sonarr_headers(),
                timeout=2,
            )
            if res.status_code == 200:
                sonarr_status = "Connected"
            else:
                sonarr_status = f"Error ({res.status_code})"
        except Exception:
            sonarr_status = "Unreachable"

    # Radarr check
    radarr_status = "Disconnected"
    if radarr_url and radarr_api_key:
        try:
            res = requests.get(
                f"{radarr_url}/api/v3/system/status",
                headers=get_radarr_headers(),
                timeout=2,
            )
            if res.status_code == 200:
                radarr_status = "Connected"
            else:
                radarr_status = f"Error ({res.status_code})"
        except Exception:
            radarr_status = "Unreachable"

    # Plex check
    plex_status = "Disconnected"
    if plex_url and plex_token:
        try:
            res = requests.get(
                f"{plex_url}/identity",
                headers={'X-Plex-Token': plex_token, 'Accept': 'application/json'},
                timeout=2,
            )
            if res.status_code == 200:
                plex_status = "Connected"
            else:
                plex_status = f"Error ({res.status_code})"
        except Exception:
            plex_status = "Unreachable"

    return {
        'sonarr': {'status': sonarr_status, 'ok': sonarr_status == 'Connected'},
        'radarr': {'status': radarr_status, 'ok': radarr_status == 'Connected'},
        'plex':   {'status': plex_status,   'ok': plex_status == 'Connected'},
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    cfg = config_store.get_all_config()
    conns = get_service_connections()

    sonarr_api_key = get_sonarr_api_key()
    radarr_api_key = get_radarr_api_key()

    # Mask API key Sonarr
    if sonarr_api_key:
        masked_key = "*" * (len(sonarr_api_key) - 4) + sonarr_api_key[-4:] if len(sonarr_api_key) >= 4 else sonarr_api_key
    else:
        masked_key = "Not Configured"

    # Mask API key Radarr
    if radarr_api_key:
        radarr_masked_key = "*" * (len(radarr_api_key) - 4) + radarr_api_key[-4:] if len(radarr_api_key) >= 4 else radarr_api_key
    else:
        radarr_masked_key = "Not Configured"

    with history_lock:
        history_list = list(webhook_history)

    nzbget_status = get_nzbget_status()
    nzbget_pwd = get_nzbget_password()
    if nzbget_pwd:
        nzbget_masked_pass = "*" * (len(nzbget_pwd) - 4) + nzbget_pwd[-4:] if len(nzbget_pwd) >= 4 else nzbget_pwd
    else:
        nzbget_masked_pass = "Not Configured"

    return render_template(
        "index.html",
        cfg=cfg,
        sonarr_url=get_sonarr_url() or "Not Configured",
        masked_key=masked_key,
        status_text=conns['sonarr']['status'],
        radarr_url=get_radarr_url() or "Not Configured",
        radarr_masked_key=radarr_masked_key,
        radarr_status_text=conns['radarr']['status'],
        plex_conn_text=conns['plex']['status'],
        nzbget_url=get_nzbget_url() or "Not Configured",
        nzbget_username=get_nzbget_username() or "Not Configured",
        nzbget_masked_pass=nzbget_masked_pass,
        nzbget_status=nzbget_status,
        history=history_list,
        rolling_window=get_rolling_window(),
        plex_status=plex_watcher.get_state(),
        poller_status=plex_poller.get_state(),
    )


@app.route('/api/config/save', methods=['POST'])
def api_config_save():
    try:
        data = request.json or {}
        updated = config_store.save_config(data)
        logger.info("[App] Saved updated encrypted configuration via UI.")
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


@app.route('/api/poller-reset-counter', methods=['POST'])
def api_poller_reset_counter():
    res = plex_poller.reset_counter()
    return jsonify(res), 200


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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
