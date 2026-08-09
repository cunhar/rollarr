from __future__ import annotations

import re
import requests
import logging
from config_store import get_config

logger = logging.getLogger(__name__)

def _clean_title(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', t)
    return re.sub(r'[^a-z0-9]', '', t.lower())

def get_sonarr_url() -> str:
    return (get_config('SONARR_URL') or '').rstrip('/')

def get_sonarr_api_key() -> str:
    return get_config('SONARR_API_KEY') or ''

def get_rolling_window() -> int:
    return int(get_config('ROLLING_WINDOW', 3))

def get_sonarr_headers():
    api_key = get_sonarr_api_key()
    if not api_key:
        raise ValueError("SONARR_API_KEY is not configured")
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json"
    }

def find_series_id_by_title(title):
    sonarr_url = get_sonarr_url()
    if not sonarr_url:
        raise ValueError("SONARR_URL is not configured")
    
    url = f"{sonarr_url}/api/v3/series"
    logger.info(f"Fetching all series from Sonarr to resolve title: '{title}'")
    
    try:
        response = requests.get(url, headers=get_sonarr_headers(), timeout=10)
        response.raise_for_status()
        series_list = response.json()
        
        normalized_target = title.lower().strip()
        target_clean = _clean_title(title)

        # Pass 1: Exact matches (title, cleanTitle, alternateTitles)
        for series in series_list:
            s_title = series.get('title', '').lower().strip()
            s_clean = series.get('cleanTitle', '').lower().strip()
            s_sort = series.get('sortTitle', '').lower().strip()
            
            if normalized_target in (s_title, s_clean, s_sort):
                logger.info(f"Resolved title '{title}' to Sonarr seriesId {series.get('id')} ('{series.get('title')}')")
                return series.get('id'), series.get('title')
                
            for alt in series.get('alternateTitles', []):
                if alt.get('title', '').lower().strip() == normalized_target:
                    logger.info(f"Resolved title '{title}' via alternateTitle to Sonarr seriesId {series.get('id')}")
                    return series.get('id'), series.get('title')

        # Pass 2: Clean alphanumeric match (ignoring punctuation, spaces, year suffixes)
        if target_clean:
            for series in series_list:
                candidates = [
                    _clean_title(series.get('title')),
                    _clean_title(series.get('sortTitle')),
                    _clean_title(series.get('cleanTitle')),
                ]
                for alt in series.get('alternateTitles', []):
                    candidates.append(_clean_title(alt.get('title')))

                if target_clean in candidates:
                    logger.info(f"Resolved title '{title}' via clean title match to Sonarr seriesId {series.get('id')} ('{series.get('title')}')")
                    return series.get('id'), series.get('title')

        logger.warning(f"Could not find a series with title '{title}' in Sonarr library")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching series list from Sonarr by title: {e}")
        raise

def find_series_id_by_tvdb_id(tvdb_id):
    """Fetch all series from Sonarr and find the internal seriesId corresponding to tvdb_id."""
    sonarr_url = get_sonarr_url()
    if not sonarr_url:
        raise ValueError("SONARR_URL is not configured")
    
    url = f"{sonarr_url}/api/v3/series"
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
    sonarr_url = get_sonarr_url()
    if not sonarr_url:
        raise ValueError("SONARR_URL is not configured")
    
    url = f"{sonarr_url}/api/v3/series/{series_id}"
    try:
        response = requests.get(url, headers=get_sonarr_headers(), timeout=10)
        response.raise_for_status()
        return response.json().get('title')
    except Exception as e:
        logger.error(f"Error fetching series details for ID {series_id}: {e}")
        return f"ID {series_id}"

def get_episodes(series_id):
    """Fetch all episodes for a given series ID."""
    sonarr_url = get_sonarr_url()
    if not sonarr_url:
        raise ValueError("SONARR_URL is not configured")
        
    url = f"{sonarr_url}/api/v3/episode"
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
    """Set monitored = true for the given episode ID."""
    sonarr_url = get_sonarr_url()
    if not sonarr_url:
        raise ValueError("SONARR_URL is not configured")
        
    url = f"{sonarr_url}/api/v3/episode/monitor"
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
    """Trigger Sonarr search command for the given episode ID."""
    sonarr_url = get_sonarr_url()
    if not sonarr_url:
        raise ValueError("SONARR_URL is not configured")
        
    url = f"{sonarr_url}/api/v3/command"
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
