from __future__ import annotations

import logging
import requests
from config_store import get_config
from integrations.common import clean_title as _clean_title, get_arr_headers, arr_request, match_media_by_title

logger = logging.getLogger(__name__)


def get_radarr_url() -> str:
    return (get_config('RADARR_URL') or '').rstrip('/')


def get_radarr_api_key() -> str:
    return get_config('RADARR_API_KEY') or ''


def get_radarr_headers() -> dict[str, str]:
    return get_arr_headers(get_radarr_api_key())


def _radarr_request(method: str, path: str, params: dict = None, json_data: dict = None) -> requests.Response:
    url = get_radarr_url()
    if not url:
        raise ValueError("RADARR_URL is not configured")
    full_url = f"{url}{path}"
    return arr_request(method, full_url, get_radarr_headers(), params=params, json_data=json_data)


def find_movie_by_title_and_year(title: str, year: int | str | None = None) -> tuple[int | None, str | None]:
    logger.info(f"Fetching movies from Radarr to resolve: '{title}' ({year})")
    try:
        response = _radarr_request("GET", "/api/v3/movie")
        movies = response.json()
        movie_id, movie_title = match_media_by_title(
            movies,
            title,
            year=year,
            additional_fields=('cleanTitle', 'originalTitle'),
        )
        if not movie_id:
            logger.warning(f"Could not find movie '{title}' in Radarr library")
        return movie_id, movie_title
    except Exception as e:
        logger.error(f"Error fetching movie list from Radarr: {e}")
        raise


def unmonitor_and_delete_movie(movie_id: int) -> tuple[bool, str]:
    try:
        res = _radarr_request("GET", f"/api/v3/movie/{movie_id}")
        movie = res.json()

        # Unmonitor if currently monitored
        if movie.get('monitored', True):
            movie['monitored'] = False
            _radarr_request("PUT", f"/api/v3/movie/{movie_id}", json_data=movie)
            logger.info(f"Unmonitored movie ID {movie_id} in Radarr")

        # Delete movie file if present
        movie_file_id = movie.get('movieFileId', 0)
        if movie_file_id and movie_file_id > 0:
            _radarr_request("DELETE", f"/api/v3/moviefile/{movie_file_id}")
            logger.info(f"Deleted movie file ID {movie_file_id} for movie ID {movie_id}")
            return True, "Unmonitored and deleted movie file from disk"
        else:
            return True, "Unmonitored movie (no movie file found on disk)"
    except Exception as e:
        logger.error(f"Error unmonitoring/deleting movie ID {movie_id} in Radarr: {e}")
        raise


def unmonitor_movie(movie_id: int) -> tuple[bool, str]:
    """Unmonitor a movie in Radarr without deleting its file from disk."""
    try:
        res = _radarr_request("GET", f"/api/v3/movie/{movie_id}")
        movie = res.json()

        if movie.get('monitored', True):
            movie['monitored'] = False
            _radarr_request("PUT", f"/api/v3/movie/{movie_id}", json_data=movie)
            logger.info(f"Unmonitored movie ID {movie_id} in Radarr (file kept on disk)")

        return True, "Unmonitored movie (file kept on disk, delete disabled)"
    except Exception as e:
        logger.error(f"Error unmonitoring movie ID {movie_id} in Radarr: {e}")
        raise
