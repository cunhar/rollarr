import os
import requests
import logging

logger = logging.getLogger(__name__)

RADARR_URL = os.environ.get('RADARR_URL')
RADARR_API_KEY = os.environ.get('RADARR_API_KEY')

def get_radarr_headers():
    if not RADARR_API_KEY:
        raise ValueError("RADARR_API_KEY environment variable is not set")
    return {
        "X-Api-Key": RADARR_API_KEY,
        "Content-Type": "application/json"
    }

def find_movie_by_title_and_year(title, year=None):
    if not RADARR_URL:
        raise ValueError("RADARR_URL environment variable is not set")
    
    url = f"{RADARR_URL.rstrip('/')}/api/v3/movie"
    logger.info(f"Fetching movies from Radarr to resolve: '{title}' ({year})")
    
    try:
        response = requests.get(url, headers=get_radarr_headers(), timeout=10)
        response.raise_for_status()
        movies = response.json()
        
        target = title.lower().strip()
        for movie in movies:
            m_title = movie.get('title', '').lower().strip()
            m_year = movie.get('year')
            if m_title == target:
                if year is None or m_year is None or int(m_year) == int(year):
                    logger.info(f"Resolved movie '{title}' to Radarr movieId {movie.get('id')}")
                    return movie.get('id'), movie.get('title')
                    
        logger.warning(f"Could not find movie '{title}' in Radarr library")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching movie list from Radarr: {e}")
        raise

def unmonitor_and_delete_movie(movie_id):
    if not RADARR_URL:
        raise ValueError("RADARR_URL environment variable is not set")
        
    url = f"{RADARR_URL.rstrip('/')}/api/v3/movie/{movie_id}"
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
            file_url = f"{RADARR_URL.rstrip('/')}/api/v3/moviefile/{movie_file_id}"
            del_res = requests.delete(file_url, headers=get_radarr_headers(), timeout=10)
            del_res.raise_for_status()
            logger.info(f"Deleted movie file ID {movie_file_id} for movie ID {movie_id}")
            return True, "Unmonitored and deleted movie file from disk"
        else:
            return True, "Unmonitored movie (no movie file found on disk)"
    except Exception as e:
        logger.error(f"Error unmonitoring/deleting movie ID {movie_id} in Radarr: {e}")
        raise
