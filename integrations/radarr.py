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

def get_radarr_url() -> str:
    return (get_config('RADARR_URL') or '').rstrip('/')

def get_radarr_api_key() -> str:
    return get_config('RADARR_API_KEY') or ''

def get_radarr_headers():
    api_key = get_radarr_api_key()
    if not api_key:
        raise ValueError("RADARR_API_KEY is not configured")
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json"
    }

def find_movie_by_title_and_year(title, year=None):
    radarr_url = get_radarr_url()
    if not radarr_url:
        raise ValueError("RADARR_URL is not configured")
    
    url = f"{radarr_url}/api/v3/movie"
    logger.info(f"Fetching movies from Radarr to resolve: '{title}' ({year})")
    
    try:
        response = requests.get(url, headers=get_radarr_headers(), timeout=10)
        response.raise_for_status()
        movies = response.json()
        
        target = title.lower().strip()
        target_clean = _clean_title(title)

        # Pass 1: Exact matches with year check
        for movie in movies:
            m_title = movie.get('title', '').lower().strip()
            m_clean = movie.get('cleanTitle', '').lower().strip()
            m_orig = movie.get('originalTitle', '').lower().strip()
            m_year = movie.get('year')

            year_matches = (year is None or m_year is None or int(m_year) == int(year))
            
            if target in (m_title, m_clean, m_orig) and year_matches:
                logger.info(f"Resolved movie '{title}' to Radarr movieId {movie.get('id')} ('{movie.get('title')}')")
                return movie.get('id'), movie.get('title')

            for alt in movie.get('alternateTitles', []):
                if alt.get('title', '').lower().strip() == target and year_matches:
                    logger.info(f"Resolved movie '{title}' via alternateTitle to Radarr movieId {movie.get('id')}")
                    return movie.get('id'), movie.get('title')

        # Pass 2: Clean alphanumeric match
        if target_clean:
            for movie in movies:
                m_year = movie.get('year')
                year_matches = (year is None or m_year is None or int(m_year) == int(year))
                
                if not year_matches:
                    continue

                candidates = [
                    _clean_title(movie.get('title')),
                    _clean_title(movie.get('cleanTitle')),
                    _clean_title(movie.get('originalTitle')),
                ]
                for alt in movie.get('alternateTitles', []):
                    candidates.append(_clean_title(alt.get('title')))

                if target_clean in candidates:
                    logger.info(f"Resolved movie '{title}' via clean title match to Radarr movieId {movie.get('id')} ('{movie.get('title')}')")
                    return movie.get('id'), movie.get('title')

        logger.warning(f"Could not find movie '{title}' in Radarr library")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching movie list from Radarr: {e}")
        raise

def unmonitor_and_delete_movie(movie_id):
    radarr_url = get_radarr_url()
    if not radarr_url:
        raise ValueError("RADARR_URL is not configured")

    url = f"{radarr_url}/api/v3/movie/{movie_id}"
    try:
        res = requests.get(url, headers=get_radarr_headers(), timeout=10)
        res.raise_for_status()
        movie = res.json()

        # Unmonitor if currently monitored
        if movie.get('monitored', True):
            movie['monitored'] = False
            put_res = requests.put(url, headers=get_radarr_headers(), json=movie, timeout=10)
            put_res.raise_for_status()
            logger.info(f"Unmonitored movie ID {movie_id} in Radarr")

        # Delete movie file if present
        movie_file_id = movie.get('movieFileId', 0)
        if movie_file_id and movie_file_id > 0:
            file_url = f"{radarr_url}/api/v3/moviefile/{movie_file_id}"
            del_res = requests.delete(file_url, headers=get_radarr_headers(), timeout=10)
            del_res.raise_for_status()
            logger.info(f"Deleted movie file ID {movie_file_id} for movie ID {movie_id}")
            return True, "Unmonitored and deleted movie file from disk"
        else:
            return True, "Unmonitored movie (no movie file found on disk)"
    except Exception as e:
        logger.error(f"Error unmonitoring/deleting movie ID {movie_id} in Radarr: {e}")
        raise


def unmonitor_movie(movie_id):
    """Unmonitor a movie in Radarr without deleting its file from disk."""
    radarr_url = get_radarr_url()
    if not radarr_url:
        raise ValueError("RADARR_URL is not configured")

    url = f"{radarr_url}/api/v3/movie/{movie_id}"
    try:
        res = requests.get(url, headers=get_radarr_headers(), timeout=10)
        res.raise_for_status()
        movie = res.json()

        if movie.get('monitored', True):
            movie['monitored'] = False
            put_res = requests.put(url, headers=get_radarr_headers(), json=movie, timeout=10)
            put_res.raise_for_status()
            logger.info(f"Unmonitored movie ID {movie_id} in Radarr (file kept on disk)")

        return True, "Unmonitored movie (file kept on disk, delete disabled)"
    except Exception as e:
        logger.error(f"Error unmonitoring movie ID {movie_id} in Radarr: {e}")
        raise
