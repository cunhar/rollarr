from flask import Flask, request, jsonify, render_template
import requests
import os
import logging
from logging.handlers import RotatingFileHandler
import json
from collections import deque
import datetime
import threading

# Import custom modular components
from utils import try_parse_int, parse_episodes
import plex_watcher
from sonarr_api import (
    SONARR_URL,
    SONARR_API_KEY,
    ROLLING_WINDOW,
    get_sonarr_headers,
    find_series_id_by_title,
    find_series_id_by_tvdb_id,
    get_series_title,
    get_episodes,
    monitor_episode,
    search_episode
)

# Persistent configuration directories
CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

HISTORY_FILE = os.path.join(CONFIG_DIR, 'history.json')
LOG_FILE = os.path.join(CONFIG_DIR, 'rolarr.log')

# Configure logging to write to both stdout/stderr and file
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

# Console logging handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# File logging handler
try:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"Could not initialize file logging: {e}")

app = Flask(__name__)

# In-memory history buffer (holds the last 20 calls) with thread lock
webhook_history = deque(maxlen=20)
history_lock = threading.Lock()

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                with history_lock:
                    webhook_history.clear()
                    for entry in reversed(data):
                        webhook_history.appendleft(entry)
            logger.info("Loaded webhook history from persistent storage.")
        except Exception as e:
            logger.error(f"Failed to load webhook history: {e}")

def save_history():
    try:
        with history_lock:
            data = list(webhook_history)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save webhook history: {e}")

# Load persistent history on startup
load_history()

# Start Plex watcher background thread
plex_watcher.start()

def log_call(status, message, payload=None):
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "message": message,
        "payload": payload
    }
    with history_lock:
        webhook_history.appendleft(entry)
    save_history()

@app.route('/')
def index():
    # Mask API key (show only last 4 chars)
    if SONARR_API_KEY:
        masked_key = "*" * (len(SONARR_API_KEY) - 4) + SONARR_API_KEY[-4:] if len(SONARR_API_KEY) >= 4 else SONARR_API_KEY
    else:
        masked_key = "Not Configured"

    # Check connection status
    status_text = "Disconnected"
    status_color = "#ef4444"
    if SONARR_URL and SONARR_API_KEY:
        try:
            url = f"{SONARR_URL.rstrip('/')}/api/v3/system/status"
            res = requests.get(url, headers=get_sonarr_headers(), timeout=2)
            if res.status_code == 200:
                status_text = "Connected"
                status_color = "#10b981"
            else:
                status_text = f"Error ({res.status_code})"
                status_color = "#f59e0b"
        except Exception:
            status_text = "Unreachable"
            status_color = "#ef4444"

    # Retrieve current request host to display the webhook URL helper
    webhook_url = f"http://{request.host}/webhook"

    with history_lock:
        history_list = list(webhook_history)

    return render_template(
        "index.html",
        sonarr_url=SONARR_URL or "Not Configured",
        masked_key=masked_key,
        status_text=status_text,
        status_color=status_color,
        webhook_url=webhook_url,
        history=history_list,
        rolling_window=ROLLING_WINDOW,
        plex_status=plex_watcher.get_state(),
    )

@app.route('/api/plex-status')
def api_plex_status():
    """Return the current Plex watcher state as JSON (polled by the dashboard)."""
    return jsonify(plex_watcher.get_state())

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    payload = request.json or {}
    logger.info(f"Received cleanup event payload: {payload}")
    
    subject = payload.get('subject')
    message = payload.get('message')
    
    # Intercept Maintainerr test notifications
    if subject == "Test Notification" or payload.get('notification_type') == 'TEST':
        msg = "Test notification received successfully"
        logger.info(msg)
        log_call("success", msg, payload)
        return jsonify({"status": "success", "message": msg}), 200
        
    # Gather episodes to process
    ep_list = []
    
    # Check if a single episode is explicitly provided in the payload root/nesting
    series_id = payload.get('seriesId') or payload.get('series', {}).get('id')
    tvdb_id = payload.get('tvdbId') or payload.get('series', {}).get('tvdbId')
    season_num = payload.get('seasonNumber') or payload.get('episode', {}).get('seasonNumber')
    episode_num = payload.get('episodeNumber') or payload.get('episode', {}).get('episodeNumber')
    
    season_num_parsed = try_parse_int(season_num)
    episode_num_parsed = try_parse_int(episode_num)
    
    if season_num_parsed is not None and episode_num_parsed is not None:
        ep_list.append({
            "series_id": try_parse_int(series_id),
            "tvdb_id": try_parse_int(tvdb_id),
            "season": season_num_parsed,
            "episode": episode_num_parsed,
            "show": None
        })
    else:
        # Fallback to parsing message and subject
        parsed_items = []
        if message:
            logger.info(f"Missing season/episode parameters. Attempting fallback parsing on message: '{message}'")
            parsed_items = parse_episodes(message)
        if not parsed_items and subject:
            logger.info(f"Missing season/episode parameters. Attempting fallback parsing on subject: '{subject}'")
            parsed_items = parse_episodes(subject)
            
        for show_name, s_num, e_num in parsed_items:
            ep_list.append({
                "series_id": None,
                "tvdb_id": None,
                "season": s_num,
                "episode": e_num,
                "show": show_name
            })
            
    if not ep_list:
        msg = "No episodes could be parsed or found in the payload"
        logger.warning(msg)
        log_call("warning", msg, payload)
        return jsonify({"status": "warning", "message": msg}), 200
        
    try:
        results = []
        has_success = False
        
        for ep in ep_list:
            series_id_parsed = ep["series_id"]
            tvdb_id_parsed = ep["tvdb_id"]
            season_num_parsed = ep["season"]
            episode_num_parsed = ep["episode"]
            show_name = ep["show"]
            series_title = None
            
            # Resolve series by title if we have show_name but no series_id
            if not series_id_parsed and show_name:
                series_id_parsed, series_title = find_series_id_by_title(show_name)
                
            # Resolve internal Sonarr series ID from tvdbId if not resolved
            if not series_id_parsed and tvdb_id_parsed:
                logger.info(f"Resolving Sonarr seriesId from tvdbId: {tvdb_id_parsed}")
                series_id_parsed, series_title = find_series_id_by_tvdb_id(tvdb_id_parsed)
                
            if not series_id_parsed:
                err_msg = f"Could not resolve series ID for show '{show_name or 'Unknown'}'"
                logger.error(err_msg)
                results.append({
                    "episode": f"S{season_num_parsed}E{episode_num_parsed}",
                    "status": "error",
                    "message": err_msg
                })
                continue
                
            # Get series title if not already retrieved
            if not series_title:
                series_title = get_series_title(series_id_parsed)
                
            # Fetch episodes list
            episodes = get_episodes(series_id_parsed)
            regular_episodes = [e for e in episodes if e.get('seasonNumber', 0) > 0]
            regular_episodes.sort(key=lambda x: (x.get('seasonNumber', 0), x.get('episodeNumber', 0)))
            
            current_index = None
            for i, e in enumerate(regular_episodes):
                if e.get('seasonNumber') == season_num_parsed and e.get('episodeNumber') == episode_num_parsed:
                    current_index = i
                    break
                    
            if current_index is None:
                warn_msg = f"Episode S{season_num_parsed}E{episode_num_parsed} not found in Sonarr series '{series_title}'"
                logger.warning(warn_msg)
                results.append({
                    "show": series_title,
                    "episode": f"S{season_num_parsed}E{episode_num_parsed}",
                    "status": "warning",
                    "message": warn_msg
                })
                continue
                
            # Get next up to ROLLING_WINDOW episodes
            next_episodes = regular_episodes[current_index + 1 : current_index + 1 + ROLLING_WINDOW]
            
            if next_episodes:
                newly_monitored = []
                already_monitored = []
                for e in next_episodes:
                    ep_id = e.get('id')
                    s_num = e.get('seasonNumber')
                    e_num = e.get('episodeNumber')
                    ep_str = f"S{s_num}E{e_num}"
                    if not e.get('monitored'):
                        monitor_episode(ep_id)
                        search_episode(ep_id)
                        newly_monitored.append(ep_str)
                    else:
                        already_monitored.append(ep_str)
                
                success_msg = f"Ensured next {ROLLING_WINDOW} episodes are monitored. Newly: {newly_monitored}, Already: {already_monitored}"
                results.append({
                    "show": series_title,
                    "episode": f"S{season_num_parsed}E{episode_num_parsed}",
                    "status": "success",
                    "message": success_msg,
                    "newlyMonitored": newly_monitored,
                    "alreadyMonitored": already_monitored
                })
                has_success = True
            else:
                final_msg = f"Final episode reached. No further episodes to monitor."
                results.append({
                    "show": series_title,
                    "episode": f"S{season_num_parsed}E{episode_num_parsed}",
                    "status": "success",
                    "message": final_msg
                })
                has_success = True
                
        summary_messages = [f"{r.get('show', 'Unknown')} ({r['episode']}): {r['message']}" for r in results]
        full_message = " | ".join(summary_messages)
        
        overall_status = "success" if has_success else "error"
        if not has_success:
            if any(r['status'] == 'warning' for r in results):
                overall_status = "warning"
                
        log_call(overall_status, full_message, payload)
        
        return jsonify({
            "status": overall_status,
            "message": full_message,
            "results": results
        }), 200 if overall_status != "error" else 400
        
    except Exception as e:
        msg = f"Failed to process webhook: {str(e)}"
        logger.error(msg)
        log_call("error", msg, payload)
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # Ensure URL and API Key are present when starting (warn if missing)
    if not SONARR_URL or not SONARR_API_KEY:
        logger.warning("SONARR_URL or SONARR_API_KEY is not defined in environment variables.")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
