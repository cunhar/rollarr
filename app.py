from flask import Flask, request, jsonify, render_template
import requests
import os
import logging
from collections import deque
import datetime

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

SONARR_URL = os.environ.get('SONARR_URL')
SONARR_API_KEY = os.environ.get('SONARR_API_KEY')

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
        history=list(webhook_history)
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
    
    if season_num is None or episode_num is None:
        msg = "Missing seasonNumber or episodeNumber"
        logger.error(msg)
        log_call("error", msg, payload)
        return jsonify({"status": "error", "message": msg}), 400
        
    try:
        # Resolve internal Sonarr series ID
        if not series_id and tvdb_id:
            series_id = find_series_id_by_tvdb_id(int(tvdb_id))
            
        if not series_id:
            msg = "Could not resolve series ID (neither seriesId nor tvdbId matched)"
            logger.error(msg)
            log_call("error", msg, payload)
            return jsonify({"status": "error", "message": msg}), 400
            
        # Get all episodes
        episodes = get_episodes(series_id)
        
        # Filter out specials (season 0) and sort by (seasonNumber, episodeNumber)
        regular_episodes = [ep for ep in episodes if ep.get('seasonNumber', 0) > 0]
        regular_episodes.sort(key=lambda x: (x.get('seasonNumber', 0), x.get('episodeNumber', 0)))
        
        # Locate the index of the deleted episode
        current_index = None
        for i, ep in enumerate(regular_episodes):
            if ep.get('seasonNumber') == int(season_num) and ep.get('episodeNumber') == int(episode_num):
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
            
        # Check if there is a next sequential episode
        if current_index + 1 < len(regular_episodes):
            next_episode = regular_episodes[current_index + 1]
            next_ep_id = next_episode.get('id')
            next_s = next_episode.get('seasonNumber')
            next_e = next_episode.get('episodeNumber')
            
            logger.info(f"Found next sequential episode: S{next_s}E{next_e} (episodeId: {next_ep_id})")
            
            # Monitor next episode
            monitor_episode(next_ep_id)
            
            # Search next episode
            search_episode(next_ep_id)
            
            msg = f"Monitored and searched next episode S{next_s}E{next_e}"
            log_call("success", msg, payload)
            return jsonify({
                "status": "success",
                "message": msg,
                "nextEpisode": {
                    "id": next_ep_id,
                    "seasonNumber": next_s,
                    "episodeNumber": next_e
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
