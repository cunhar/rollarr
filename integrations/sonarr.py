from __future__ import annotations

import logging
import requests
from config_store import get_config
from integrations.common import clean_title as _clean_title, get_arr_headers, arr_request, match_media_by_title

logger = logging.getLogger(__name__)


def get_sonarr_url() -> str:
    return (get_config('SONARR_URL') or '').rstrip('/')


def get_sonarr_api_key() -> str:
    return get_config('SONARR_API_KEY') or ''


def get_rolling_window() -> int:
    return int(get_config('ROLLING_WINDOW', 3))


def get_sonarr_headers() -> dict[str, str]:
    return get_arr_headers(get_sonarr_api_key())


def _sonarr_request(method: str, path: str, params: dict = None, json_data: dict = None) -> requests.Response:
    url = get_sonarr_url()
    if not url:
        raise ValueError("SONARR_URL is not configured")
    full_url = f"{url}{path}"
    return arr_request(method, full_url, get_sonarr_headers(), params=params, json_data=json_data)


def find_series_id_by_title(title: str) -> tuple[int | None, str | None]:
    logger.info(f"Fetching all series from Sonarr to resolve title: '{title}'")
    try:
        response = _sonarr_request("GET", "/api/v3/series")
        series_list = response.json()
        series_id, series_title = match_media_by_title(
            series_list,
            title,
            additional_fields=('sortTitle', 'cleanTitle'),
        )
        if not series_id:
            logger.warning(f"Could not find a series with title '{title}' in Sonarr library")
        return series_id, series_title
    except Exception as e:
        logger.error(f"Error fetching series list from Sonarr by title: {e}")
        raise


def find_series_id_by_tvdb_id(tvdb_id: int) -> tuple[int | None, str | None]:
    """Fetch all series from Sonarr and find the internal seriesId corresponding to tvdb_id."""
    logger.info(f"Fetching all series from Sonarr to resolve tvdbId: {tvdb_id}")
    try:
        response = _sonarr_request("GET", "/api/v3/series")
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


def get_series_title(series_id: int) -> str:
    try:
        response = _sonarr_request("GET", f"/api/v3/series/{series_id}")
        return response.json().get('title')
    except Exception as e:
        logger.error(f"Error fetching series details for ID {series_id}: {e}")
        return f"ID {series_id}"


def get_episodes(series_id: int) -> list[dict]:
    """Fetch all episodes for a given series ID."""
    logger.info(f"Fetching episodes for seriesId: {series_id}")
    try:
        response = _sonarr_request("GET", "/api/v3/episode", params={"seriesId": series_id})
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching episodes from Sonarr: {e}")
        raise


def monitor_episode(episode_id: int) -> bool:
    """Set monitored = true for the given episode ID."""
    logger.info(f"Monitoring episodeId: {episode_id}")
    try:
        _sonarr_request("PUT", "/api/v3/episode/monitor", json_data={"episodeIds": [episode_id], "monitored": True})
        logger.info(f"Successfully monitored episodeId: {episode_id}")
        return True
    except Exception as e:
        logger.error(f"Error monitoring episodeId {episode_id}: {e}")
        raise


def search_episode(episode_id: int) -> bool:
    """Trigger Sonarr search command for the given episode ID."""
    logger.info(f"Triggering search command for episodeId: {episode_id}")
    try:
        _sonarr_request("POST", "/api/v3/command", json_data={"name": "EpisodeSearch", "episodeIds": [episode_id]})
        logger.info(f"Successfully triggered search for episodeId: {episode_id}")
        return True
    except Exception as e:
        logger.error(f"Error triggering search for episodeId {episode_id}: {e}")
        raise


def unmonitor_episode(episode_id: int) -> bool:
    """Set monitored = false for the given episode ID."""
    logger.info(f"Unmonitoring episodeId: {episode_id}")
    try:
        _sonarr_request("PUT", "/api/v3/episode/monitor", json_data={"episodeIds": [episode_id], "monitored": False})
        logger.info(f"Successfully unmonitored episodeId: {episode_id}")
        return True
    except Exception as e:
        logger.error(f"Error unmonitoring episodeId {episode_id}: {e}")
        raise


def delete_episode_file(episode_file_id: int) -> bool:
    """Delete the episode media file from disk via Sonarr API."""
    logger.info(f"Deleting episode file ID {episode_file_id} in Sonarr")
    try:
        _sonarr_request("DELETE", f"/api/v3/episodefile/{episode_file_id}")
        logger.info(f"Successfully deleted episode file ID {episode_file_id} from disk")
        return True
    except Exception as e:
        logger.error(f"Error deleting episode file ID {episode_file_id}: {e}")
        raise
