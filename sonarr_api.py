import os
import requests
import logging

logger = logging.getLogger(__name__)

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
                return series.get('id'), series.get('title')
                
        logger.warning(f"Could not find a series with title '{title}' in Sonarr library")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching series list from Sonarr by title: {e}")
        raise

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
                return series.get('id'), series.get('title')
                
        logger.warning(f"Could not find a series with tvdbId {tvdb_id} in Sonarr library")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching series list from Sonarr: {e}")
        raise

def get_series_title(series_id):
    if not SONARR_URL:
        raise ValueError("SONARR_URL environment variable is not set")
    
    url = f"{SONARR_URL.rstrip('/')}/api/v3/series/{series_id}"
    try:
        response = requests.get(url, headers=get_sonarr_headers(), timeout=10)
        response.raise_for_status()
        return response.json().get('title')
    except Exception as e:
        logger.error(f"Error fetching series details for ID {series_id}: {e}")
        return f"ID {series_id}"

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
