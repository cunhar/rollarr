from flask import Flask, request, jsonify, render_template
import requests
import os
import logging
from collections import deque
import datetime
import threading

# Import custom modular components
from utils import try_parse_int, parse_subject_title
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

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# In-memory history buffer (holds the last 20 calls) with thread lock
webhook_history = deque(maxlen=20)
history_lock = threading.Lock()

def log_call(status, message, payload=None):
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "message": message,
        "payload": payload
    }
    with history_lock:
        webhook_history.appendleft(entry)

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
        rolling_window=ROLLING_WINDOW
    )

@app.route('/webhook', methods=['POST'])
def handle_webhook():
    payload = request.json or {}
    logger.info(f"Received cleanup event payload: {payload}")
    
    # Extract parameters supporting various common nesting structures
    series_id = payload.get('seriesId') or payload.get('series', {}).get('id')
    tvdb_id = payload.get('tvdbId') or payload.get('series', {}).get('tvdbId')
    season_num = payload.get('seasonNumber') or payload.get('episode', {}).get('seasonNumber')
    episode_num = payload.get('episodeNumber') or payload.get('episode', {}).get('episodeNumber')
    subject = payload.get('subject') or payload.get('message')
    
    # Intercept Maintainerr test notifications
    if subject == "Test Notification" or payload.get('notification_type') == 'TEST':
        msg = "Test notification received successfully"
        logger.info(msg)
        log_call("success", msg, payload)
        return jsonify({"status": "success", "message": msg}), 200
    
    series_id_parsed = try_parse_int(series_id)
    tvdb_id_parsed = try_parse_int(tvdb_id)
    season_num_parsed = try_parse_int(season_num)
    episode_num_parsed = try_parse_int(episode_num)
    series_title = None
    
    # Fallback to subject parsing if season/episode are missing
    if (season_num_parsed is None or episode_num_parsed is None) and subject:
        logger.info(f"Missing season/episode parameters. Attempting fallback parsing on subject: '{subject}'")
        parsed = parse_subject_title(subject)
        if parsed:
            show_name, s_num, e_num = parsed
            season_num_parsed = s_num
            episode_num_parsed = e_num
            logger.info(f"Successfully parsed subject into: Show='{show_name}', S{s_num}E{e_num}")
            
            # Resolve series by title
            if not series_id_parsed:
                series_id_parsed, series_title = find_series_id_by_title(show_name)
        else:
            logger.warning(f"Could not parse show/season/episode pattern from subject: '{subject}'")
            
    if season_num_parsed is None or episode_num_parsed is None:
        msg = f"Missing or invalid seasonNumber ({season_num}) or episodeNumber ({episode_num})"
        logger.error(msg)
        log_call("error", msg, payload)
        return jsonify({"status": "error", "message": msg}), 400
        
    try:
        # Resolve internal Sonarr series ID
        resolved_series_id = series_id_parsed
        if not resolved_series_id and tvdb_id_parsed:
            logger.info(f"Resolving Sonarr seriesId from tvdbId: {tvdb_id_parsed}")
            resolved_series_id, series_title = find_series_id_by_tvdb_id(tvdb_id_parsed)
            
        if not resolved_series_id:
            msg = f"Could not resolve series ID (neither seriesId nor tvdbId matched)"
            logger.error(msg)
            log_call("error", msg, payload)
            return jsonify({"status": "error", "message": msg}), 400
            
        # Get series title if not already retrieved
        if not series_title:
            logger.info(f"Retrieving title for series ID {resolved_series_id} from Sonarr...")
            series_title = get_series_title(resolved_series_id)
            
        logger.info(f"Show matched: '{series_title}' (Sonarr ID: {resolved_series_id})")
            
        # Get all episodes
        logger.info(f"Fetching episodes list for '{series_title}' from Sonarr...")
        episodes = get_episodes(resolved_series_id)
        
        # Filter out specials (season 0) and sort by (seasonNumber, episodeNumber)
        regular_episodes = [ep for ep in episodes if ep.get('seasonNumber', 0) > 0]
        regular_episodes.sort(key=lambda x: (x.get('seasonNumber', 0), x.get('episodeNumber', 0)))
        logger.info(f"Found {len(regular_episodes)} regular episodes for '{series_title}'")
        
        # Locate the index of the deleted episode
        logger.info(f"Locating index of triggering episode S{season_num_parsed}E{episode_num_parsed}...")
        current_index = None
        for i, ep in enumerate(regular_episodes):
            if ep.get('seasonNumber') == season_num_parsed and ep.get('episodeNumber') == episode_num_parsed:
                current_index = i
                break
                
        if current_index is None:
            msg = f"Episode S{season_num_parsed}E{episode_num_parsed} not found in Sonarr series '{series_title}'"
            logger.warning(msg)
            log_call("warning", msg, payload)
            return jsonify({
                "status": "warning", 
                "message": msg
            }), 200
            
        logger.info(f"Triggering episode S{season_num_parsed}E{episode_num_parsed} located at index {current_index}")
            
        # Get next up to ROLLING_WINDOW episodes
        next_episodes = regular_episodes[current_index + 1 : current_index + 1 + ROLLING_WINDOW]
        next_ep_strs = [f"S{e.get('seasonNumber')}E{e.get('episodeNumber')}" for e in next_episodes]
        logger.info(f"Next {len(next_episodes)} episodes in rolling window: {next_ep_strs}")
        
        if next_episodes:
            newly_monitored = []
            already_monitored = []
            
            for ep in next_episodes:
                ep_id = ep.get('id')
                s_num = ep.get('seasonNumber')
                e_num = ep.get('episodeNumber')
                ep_str = f"S{s_num}E{e_num}"
                
                if not ep.get('monitored'):
                    logger.info(f"Episode {ep_str} is not monitored. Enabling monitoring and triggering search.")
                    monitor_episode(ep_id)
                    search_episode(ep_id)
                    newly_monitored.append(ep_str)
                else:
                    logger.info(f"Episode {ep_str} is already monitored.")
                    already_monitored.append(ep_str)
            
            # Construct a clear success message
            parts = []
            if newly_monitored:
                parts.append(f"Newly monitored & searched: {', '.join(newly_monitored)}")
            if already_monitored:
                parts.append(f"Already monitored: {', '.join(already_monitored)}")
            
            msg = f"Processed event for '{series_title}' (S{season_num_parsed}E{episode_num_parsed}). Ensured next {ROLLING_WINDOW} episodes are monitored. " + " | ".join(parts)
            logger.info(msg)
            log_call("success", msg, payload)
            
            return jsonify({
                "status": "success",
                "message": msg,
                "details": {
                    "seriesTitle": series_title,
                    "triggeringEpisode": f"S{season_num_parsed}E{episode_num_parsed}",
                    "newlyMonitored": newly_monitored,
                    "alreadyMonitored": already_monitored
                }
            }), 200
        else:
            msg = f"Episode '{series_title}' S{season_num_parsed}E{episode_num_parsed} was the final episode. No further episodes to monitor."
            logger.info(msg)
            log_call("success", msg, payload)
            return jsonify({
                "status": "success",
                "message": msg
            }), 200
            
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
