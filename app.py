from flask import Flask, request, jsonify, render_template
import requests
import os
import logging
from logging.handlers import RotatingFileHandler
import json
from collections import deque
import datetime
import threading

from integrations.sonarr import (
    SONARR_URL,
    SONARR_API_KEY,
    ROLLING_WINDOW,
    get_sonarr_headers,
)
from integrations.radarr import (
    RADARR_URL,
    RADARR_API_KEY,
    get_radarr_headers,
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
history_lock    = threading.Lock()


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                with history_lock:
                    webhook_history.clear()
                    for entry in reversed(data):
                        webhook_history.appendleft(entry)
            logger.info("Loaded activity history from persistent storage.")
        except Exception as e:
            logger.error(f"Failed to load activity history: {e}")


def save_history():
    try:
        with history_lock:
            data = list(webhook_history)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save activity history: {e}")


def log_call(status: str, message: str, payload=None):
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":    status,
        "message":   message,
        "payload":   payload,
    }
    with history_lock:
        webhook_history.appendleft(entry)
    save_history()


# Start background services
load_history()
plex_poller.set_log_callback(log_call)
plex_poller.start()
plex_watcher.start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    # Mask API key Sonarr
    if SONARR_API_KEY:
        masked_key = "*" * (len(SONARR_API_KEY) - 4) + SONARR_API_KEY[-4:] if len(SONARR_API_KEY) >= 4 else SONARR_API_KEY
    else:
        masked_key = "Not Configured"

    # Mask API key Radarr
    if RADARR_API_KEY:
        radarr_masked_key = "*" * (len(RADARR_API_KEY) - 4) + RADARR_API_KEY[-4:] if len(RADARR_API_KEY) >= 4 else RADARR_API_KEY
    else:
        radarr_masked_key = "Not Configured"

    # Sonarr connection check
    status_text  = "Disconnected"
    status_color = "#e05252"
    if SONARR_URL and SONARR_API_KEY:
        try:
            res = requests.get(
                f"{SONARR_URL.rstrip('/')}/api/v3/system/status",
                headers=get_sonarr_headers(),
                timeout=2,
            )
            if res.status_code == 200:
                status_text  = "Connected"
                status_color = "#3ecf8e"
            else:
                status_text  = f"Error ({res.status_code})"
                status_color = "#e5a00d"
        except Exception:
            status_text  = "Unreachable"
            status_color = "#e05252"

    # Radarr connection check
    radarr_status_text  = "Disconnected"
    radarr_status_color = "#e05252"
    if RADARR_URL and RADARR_API_KEY:
        try:
            res = requests.get(
                f"{RADARR_URL.rstrip('/')}/api/v3/system/status",
                headers=get_radarr_headers(),
                timeout=2,
            )
            if res.status_code == 200:
                radarr_status_text  = "Connected"
                radarr_status_color = "#3ecf8e"
            else:
                radarr_status_text  = f"Error ({res.status_code})"
                radarr_status_color = "#e5a00d"
        except Exception:
            radarr_status_text  = "Unreachable"
            radarr_status_color = "#e05252"

    with history_lock:
        history_list = list(webhook_history)

    return render_template(
        "index.html",
        sonarr_url=SONARR_URL or "Not Configured",
        masked_key=masked_key,
        status_text=status_text,
        status_color=status_color,
        radarr_url=RADARR_URL or "Not Configured",
        radarr_masked_key=radarr_masked_key,
        radarr_status_text=radarr_status_text,
        radarr_status_color=radarr_status_color,
        history=history_list,
        rolling_window=ROLLING_WINDOW,
        plex_status=plex_watcher.get_state(),
        poller_status=plex_poller.get_state(),
    )


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



@app.route('/api/clear-logs', methods=['POST'])
def api_clear_logs():
    with history_lock:
        webhook_history.clear()
    save_history()
    logger.info("Activity history cleared via UI.")
    return jsonify({"status": "ok", "message": "History cleared"}), 200


if __name__ == '__main__':
    if not SONARR_URL or not SONARR_API_KEY:
        logger.warning("SONARR_URL or SONARR_API_KEY is not defined in environment variables.")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
