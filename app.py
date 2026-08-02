from flask import Flask, request, jsonify, render_template_string
import requests
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

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

    template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Rolarr - Sonarr Companion</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {
                --bg: #090d16;
                --card-bg: rgba(17, 24, 39, 0.7);
                --card-border: rgba(255, 255, 255, 0.08);
                --text: #f3f4f6;
                --text-muted: #9ca3af;
                --primary: #6366f1;
                --primary-glow: rgba(99, 102, 241, 0.15);
            }
            body {
                margin: 0;
                padding: 0;
                font-family: 'Outfit', sans-serif;
                background-color: var(--bg);
                color: var(--text);
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background-image: 
                    radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.08) 0%, transparent 40%),
                    radial-gradient(circle at 90% 80%, rgba(168, 85, 247, 0.08) 0%, transparent 40%);
            }
            .container {
                width: 100%;
                max-width: 540px;
                padding: 20px;
                box-sizing: border-box;
            }
            .card {
                background: var(--card-bg);
                backdrop-filter: blur(16px);
                border: 1px solid var(--card-border);
                border-radius: 24px;
                padding: 40px;
                box-shadow: 0 20px 40px rgba(0, 0, 0, 0.3), inset 0 1px 0 rgba(255, 255, 255, 0.1);
                position: relative;
                overflow: hidden;
            }
            .card::before {
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                right: 0;
                height: 3px;
                background: linear-gradient(90deg, #6366f1, #a855f7);
            }
            .header {
                text-align: center;
                margin-bottom: 32px;
            }
            h1 {
                font-size: 32px;
                font-weight: 800;
                margin: 0 0 8px 0;
                background: linear-gradient(135deg, #fff 40%, var(--text-muted));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                letter-spacing: -0.5px;
            }
            .subtitle {
                font-size: 14px;
                color: var(--text-muted);
                margin: 0;
            }
            .status-badge {
                display: inline-flex;
                align-items: center;
                gap: 8px;
                background: rgba(255, 255, 255, 0.04);
                border: 1px solid var(--card-border);
                padding: 6px 16px;
                border-radius: 99px;
                font-size: 13px;
                font-weight: 600;
                margin-top: 12px;
            }
            .status-dot {
                width: 8px;
                height: 8px;
                background-color: {{ status_color }};
                border-radius: 50%;
                box-shadow: 0 0 12px {{ status_color }};
                animation: pulse 2s infinite;
            }
            .info-section {
                display: flex;
                flex-direction: column;
                gap: 20px;
            }
            .info-group {
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--card-border);
                border-radius: 16px;
                padding: 16px;
                transition: border-color 0.3s ease, background 0.3s ease;
            }
            .info-group:hover {
                border-color: rgba(99, 102, 241, 0.3);
                background: rgba(99, 102, 241, 0.02);
            }
            .label {
                font-size: 12px;
                font-weight: 600;
                text-transform: uppercase;
                color: var(--text-muted);
                letter-spacing: 1px;
                margin-bottom: 6px;
            }
            .value {
                font-size: 15px;
                font-family: monospace;
                color: var(--text);
                word-break: break-all;
            }
            .helper-box {
                margin-top: 18px;
                font-size: 13px;
                color: var(--text-muted);
                line-height: 1.5;
                text-align: center;
            }
            @keyframes pulse {
                0% { transform: scale(1); opacity: 1; }
                50% { transform: scale(1.2); opacity: 0.7; }
                100% { transform: scale(1); opacity: 1; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <div class="header">
                    <h1>Rolarr</h1>
                    <p class="subtitle">Rolling Window Monitor Bridge</p>
                    <div class="status-badge">
                        <span class="status-dot"></span>
                        <span style="color: {{ status_color }}">{{ status_text }}</span>
                    </div>
                </div>
                
                <div class="info-section">
                    <div class="info-group">
                        <div class="label">Sonarr URL</div>
                        <div class="value">{{ sonarr_url }}</div>
                    </div>
                    
                    <div class="info-group">
                        <div class="label">Sonarr API Key</div>
                        <div class="value">{{ masked_key }}</div>
                    </div>

                    <div class="info-group">
                        <div class="label">Webhook Endpoint</div>
                        <div class="value">{{ webhook_url }}</div>
                    </div>
                </div>

                <div class="helper-box">
                    Configure Maintainerr notifications to send HTTP POST requests to the Webhook Endpoint.
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(
        template,
        sonarr_url=SONARR_URL or "Not Configured",
        masked_key=masked_key,
        status_text=status_text,
        status_color=status_color,
        webhook_url=webhook_url
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
        logger.error("Missing seasonNumber or episodeNumber in webhook payload")
        return jsonify({"status": "error", "message": "Missing seasonNumber or episodeNumber"}), 400
        
    try:
        # Resolve internal Sonarr series ID
        if not series_id and tvdb_id:
            series_id = find_series_id_by_tvdb_id(int(tvdb_id))
            
        if not series_id:
            logger.error("Could not resolve series ID (neither seriesId nor tvdbId matched)")
            return jsonify({"status": "error", "message": "Could not resolve series ID"}), 400
            
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
            logger.warning(f"Deleted episode S{season_num}E{episode_num} not found in Sonarr episodes list")
            return jsonify({
                "status": "warning", 
                "message": f"Episode S{season_num}E{episode_num} not found in Sonarr series {series_id}"
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
            
            return jsonify({
                "status": "success",
                "message": f"Monitored and searched next episode S{next_s}E{next_e}",
                "nextEpisode": {
                    "id": next_ep_id,
                    "seasonNumber": next_s,
                    "episodeNumber": next_e
                }
            }), 200
        else:
            logger.info("The deleted episode was the final episode of the series. No next episode to monitor.")
            return jsonify({
                "status": "success",
                "message": "Deleted episode was the final episode. No further episodes to monitor."
            }), 200
            
    except Exception as e:
        logger.error(f"Failed to process webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    # Ensure URL and API Key are present when starting (warn if missing)
    if not SONARR_URL or not SONARR_API_KEY:
        logger.warning("SONARR_URL or SONARR_API_KEY is not defined in environment variables.")
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
