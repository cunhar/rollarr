from flask import Flask, request, jsonify, render_template
import requests
import os
import logging
from collections import deque
import datetime
import re

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# In-memory history buffer (holds the last 20 calls)
webhook_history = deque(maxlen=20)

def log_call(status, message, payload=None):
    entry = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "message": message,
        "payload": payload
    }
    webhook_history.appendleft(entry)

def try_parse_int(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None

def parse_subject_title(subject):
    if not subject:
        return None
    
    # Try S01E01 style first
    match = re.match(r'^(.+?)\s*-\s*[Ss](\d+)[Ee](\d+)(?:\s*-\s*(.+))?$', subject)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3))
    
    # Try 1x01 style
    match = re.match(r'^(.+?)\s*-\s*(\d+)x(\d+)(?:\s*-\s*(.+))?$', subject)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3))
        
    return None

def find_series_id_by_title(title):
    if not SONARR_URL:
        raise ValueError("SONARR_URL environment variable is not set")
    
    url = f"{SONARR_URL.rstrip('/')}/api/v3/series"
    logger.info(f"Fetching all series from Sonarr to resolve title: '{title}'")
    
    try:
        response = requests.get(url, headers=get_sonarr_headers(), timeout=10)
        response.raise_for_status()
        series_list = response.json()
        
        normalized_target = title.lower().strip()
        for series in series_list:
            if series.get('title', '').lower().strip() == normalized_target:
                logger.info(f"Resolved title '{title}' to Sonarr seriesId {series.get('id')}")
                return series.get('id')
                
        logger.warning(f"Could not find a series with title '{title}' in Sonarr library")
        return None
    except Exception as e:
        logger.error(f"Error fetching series list from Sonarr by title: {e}")
        raise

SONARR_URL = os.environ.get('SONARR_URL')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY')
ROLLING_WINDOW = int(os.environ.get('ROLLING_WINDOW', 3))

def get_sonarr_headers():
    if not SONARR_API_KEY:
        raise ValueError("SONARR_API_KEY environment variable is not set")
    return {
        "X-Api-Key": SONARR_API_KEY,
        "Content-Type": "application/json"
    }

def find_series_id_by_tvdb_id(tvdb_id):
    """
    Fetch all series from Sonarr and find the internal seriesId corresponding to tvdb_id.
    """
    if not SONARR_URL:
        raise ValueError("SONARR_URL environment variable is not set")
    
    url = f"{SONARR_URL.rstrip('/')}/api/v3/series"
    logger.info(f"Fetching all series from Sonarr to resolve tvdbId: {tvdb_id}")
    
    try:
        response = requests.get(url, headers=get_sonarr_headers(), timeout=10)
        response.raise_for_status()
        series_list = response.json()
        
        for series in series_list:
            if series.get('tvdbId') == tvdb_id:
                logger.info(f"Resolved tvdbId {tvdb_id} to Sonarr seriesId {series.get('id')}")
                return series.get('id')
                
        logger.warning(f"Could not find a series with tvdbId {tvdb_id} in Sonarr library")
        return None
    except Exception as e:
        logger.error(f"Error fetching series list from Sonarr: {e}")
        raise

def get_episodes(series_id):
    """
    Fetch all episodes for a given series ID.
    """
    if not SONARR_URL:
        raise ValueError("SONARR_URL environment variable is not set")
        
    url = f"{SONARR_URL.rstrip('/')}/api/v3/episode"
    params = {"seriesId": series_id}
    logger.info(f"Fetching episodes for seriesId: {series_id}")
    
    try:
        response = requests.get(url, headers=get_sonarr_headers(), params=params, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching episodes from Sonarr: {e}")
        raise

def monitor_episode(episode_id):
    """
    Set monitored = true for the given episode ID.
    """
    if not SONARR_URL:
        raise ValueError("SONARR_URL environment variable is not set")
        
    url = f"{SONARR_URL.rstrip('/')}/api/v3/episode/monitor"
    payload = {
        "episodeIds": [episode_id],
        "monitored": True
    }
    logger.info(f"Monitoring episodeId: {episode_id}")
    
    try:
        response = requests.put(url, headers=get_sonarr_headers(), json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Successfully monitored episodeId: {episode_id}")
        return True
    except Exception as e:
        logger.error(f"Error monitoring episodeId {episode_id}: {e}")
        raise

def search_episode(episode_id):
    """
    Trigger Sonarr search command for the given episode ID.
    """
    if not SONARR_URL:
        raise ValueError("SONARR_URL environment variable is not set")
        
    url = f"{SONARR_URL.rstrip('/')}/api/v3/command"
    payload = {
        "name": "EpisodeSearch",
        "episodeIds": [episode_id]
    }
    logger.info(f"Triggering search command for episodeId: {episode_id}")
    
    try:
        response = requests.post(url, headers=get_sonarr_headers(), json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Successfully triggered search for episodeId: {episode_id}")
        return True
    except Exception as e:
        logger.error(f"Error triggering search for episodeId {episode_id}: {e}")
        raise

@app.route('/')
def index():
    # Mask API key (show only last 4 chars)
    if SONARR_API_KEY:
        masked_key = "*" * (len(SONARR_API_KEY) - 4) + SONARR_API_KEY[-4:] if len(SONARR_API_KEY) >= 4 else SONARR_API_KEY
    else:
        masked_key = "Not Configured"

    # Check connection
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

    return render_template(
        "index.html",
        sonarr_url=SONARR_URL or "Not Configured",
        masked_key=masked_key,
        status_text=status_text,
        status_color=status_color,
        webhook_url=webhook_url,
        history=list(webhook_history),
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
                series_id_parsed = find_series_id_by_title(show_name)
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
            resolved_series_id = find_series_id_by_tvdb_id(tvdb_id_parsed)
            
        if not resolved_series_id:
            msg = f"Could not resolve series ID (neither seriesId nor tvdbId matched)"
            logger.error(msg)
            log_call("error", msg, payload)
            return jsonify({"status": "error", "message": msg}), 400
            
        # Get all episodes
        episodes = get_episodes(resolved_series_id)
        
        # Filter out specials (season 0) and sort by (seasonNumber, episodeNumber)
        regular_episodes = [ep for ep in episodes if ep.get('seasonNumber', 0) > 0]
        regular_episodes.sort(key=lambda x: (x.get('seasonNumber', 0), x.get('episodeNumber', 0)))
        
        # Locate the index of the deleted episode
        current_index = None
        for i, ep in enumerate(regular_episodes):
            if ep.get('seasonNumber') == season_num_parsed and ep.get('episodeNumber') == episode_num_parsed:
                current_index = i
                break
                
        if current_index is None:
            msg = f"Episode S{season_num}E{episode_num} not found in Sonarr series {series_id}"
            logger.warning(msg)
            log_call("warning", msg, payload)
            return jsonify({
                "status": "warning", 
                "message": msg
            }), 200
            
        # Get next up to ROLLING_WINDOW episodes
        next_episodes = regular_episodes[current_index + 1 : current_index + 1 + ROLLING_WINDOW]
        
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
            
            msg = f"Ensured next {ROLLING_WINDOW} episodes are monitored. " + " | ".join(parts)
            log_call("success", msg, payload)
            
            return jsonify({
                "status": "success",
                "message": msg,
                "details": {
                    "newlyMonitored": newly_monitored,
                    "alreadyMonitored": already_monitored
                }
            }), 200
        else:
            msg = f"Deleted episode S{season_num}E{episode_num} was the final episode. No further episodes to monitor."
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
