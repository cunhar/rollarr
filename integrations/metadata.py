"""
integrations/metadata.py
------------------------
Metadata providers for Rolarr:
  - TVMaze (free, no API key required) for TV shows & episode lists
  - TMDB (free API key) for Movies & IMDB IDs
"""
from __future__ import annotations

import time
import datetime
import logging
from typing import Any
from urllib.parse import quote

import requests

from config_store import get_config
from integrations.common import clean_title

logger = logging.getLogger(__name__)

TVMAZE_BASE = 'https://api.tvmaze.com'
TMDB_BASE = 'https://api.themoviedb.org/3'
TMDB_IMG_BASE = 'https://image.tmdb.org/t/p/w300'

# Simple in-memory TTL cache for TVMaze episode lists (30 min)
_episode_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = 1800


# ── TVMaze (TV Shows) ────────────────────────────────────────────────────────

def _format_tvmaze_show(show: dict[str, Any]) -> dict[str, Any]:
    externals = show.get('externals') or {}
    image = show.get('image') or {}
    premiered = show.get('premiered') or ''
    year = int(premiered[:4]) if len(premiered) >= 4 and premiered[:4].isdigit() else None
    return {
        'type': 'tv',
        'id': show.get('id'),
        'tvmaze_id': show.get('id'),
        'tvdb_id': externals.get('thetvdb'),
        'imdb_id': externals.get('imdb'),
        'title': show.get('name') or '',
        'year': year,
        'status': show.get('status') or '',
        'poster_url': image.get('medium') or image.get('original') or '',
        'overview': (show.get('summary') or '').replace('<p>', '').replace('</p>', '').replace('<b>', '').replace('</b>', ''),
    }


def search_tv(query: str) -> list[dict[str, Any]]:
    """Search TVMaze for TV shows matching query."""
    q = (query or '').strip()
    if not q:
        return []
    try:
        resp = requests.get(f"{TVMAZE_BASE}/search/shows", params={'q': q}, timeout=10)
        resp.raise_for_status()
        results = []
        for entry in (resp.json() or []):
            show = entry.get('show')
            if show and show.get('id'):
                results.append(_format_tvmaze_show(show))
        return results[:15]
    except Exception as exc:
        logger.warning(f"[Metadata] TVMaze search failed for '{q}': {exc}")
        return []


def get_show_by_id(tvmaze_id: int) -> dict[str, Any] | None:
    try:
        resp = requests.get(f"{TVMAZE_BASE}/shows/{int(tvmaze_id)}", timeout=10)
        resp.raise_for_status()
        return _format_tvmaze_show(resp.json())
    except Exception as exc:
        logger.warning(f"[Metadata] TVMaze show lookup failed (id={tvmaze_id}): {exc}")
        return None


def find_show_by_title(title: str) -> dict[str, Any] | None:
    """Resolve a TV show title on TVMaze using exact then clean-title matching."""
    results = search_tv(title)
    if not results:
        return None
    norm = title.lower().strip()
    target_clean = clean_title(title)

    for s in results:
        if s['title'].lower().strip() == norm:
            return s
    for s in results:
        if clean_title(s['title']) == target_clean:
            return s
    return results[0]


def get_show_episodes(tvmaze_id: int, force_refresh: bool = False) -> list[dict[str, Any]]:
    """
    Fetch regular episodes (season > 0) for a TVMaze show ID, sorted by (season, episode).
    Marks whether each episode has already aired.
    """
    now_ts = time.time()
    cached = _episode_cache.get(int(tvmaze_id))
    if cached and not force_refresh and (now_ts - cached[0]) < _CACHE_TTL:
        return cached[1]

    try:
        resp = requests.get(f"{TVMAZE_BASE}/shows/{int(tvmaze_id)}/episodes", timeout=10)
        resp.raise_for_status()
        today = datetime.date.today().isoformat()
        episodes = []
        for ep in (resp.json() or []):
            season = ep.get('season')
            number = ep.get('number')
            if not season or season <= 0 or not number or number <= 0:
                continue
            airdate = ep.get('airdate') or ''
            aired = bool(airdate and airdate <= today)
            episodes.append({
                'season': int(season),
                'episode': int(number),
                'title': ep.get('name') or '',
                'airdate': airdate,
                'aired': aired,
            })
        episodes.sort(key=lambda e: (e['season'], e['episode']))
        _episode_cache[int(tvmaze_id)] = (now_ts, episodes)
        return episodes
    except Exception as exc:
        logger.warning(f"[Metadata] TVMaze episode list failed (id={tvmaze_id}): {exc}")
        return cached[1] if cached else []


# ── TMDB (Movies) ────────────────────────────────────────────────────────────

def get_tmdb_api_key() -> str:
    return (get_config('TMDB_API_KEY') or '').strip()


def search_movies_imdb(query: str) -> list[dict[str, Any]]:
    """Keyless fallback movie search using IMDb's public suggestion endpoint."""
    q = (query or '').strip()
    if not q:
        return []
    try:
        resp = requests.get(
            f"https://v3.sg.media-imdb.com/suggestion/x/{quote(q)}.json",
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10,
        )
        resp.raise_for_status()
        results = []
        for item in (resp.json().get('d') or []):
            imdb_id = item.get('id') or ''
            qid = (item.get('qid') or item.get('q') or '').lower()
            if not imdb_id.startswith('tt'):
                continue
            if qid and qid not in ('movie', 'feature', 'tvmovie', 'video'):
                continue
            img = item.get('i') or {}
            results.append({
                'type': 'movie',
                'id': imdb_id,
                'tmdb_id': None,
                'imdb_id': imdb_id,
                'title': item.get('l') or '',
                'year': item.get('y'),
                'poster_url': img.get('imageUrl') or '',
                'overview': item.get('s') or '',
            })
        return results[:15]
    except Exception as exc:
        logger.warning(f"[Metadata] IMDb movie search failed for '{q}': {exc}")
        return []


def search_movies(query: str) -> list[dict[str, Any]]:
    """Search movies via TMDB if API key is configured, otherwise keyless IMDb search."""
    api_key = get_tmdb_api_key()
    q = (query or '').strip()
    if not q:
        return []
    if not api_key:
        return search_movies_imdb(q)
    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/movie",
            params={'api_key': api_key, 'query': q, 'include_adult': 'false'},
            timeout=10,
        )
        resp.raise_for_status()
        results = []
        for m in (resp.json().get('results') or [])[:15]:
            rel = m.get('release_date') or ''
            year = int(rel[:4]) if len(rel) >= 4 and rel[:4].isdigit() else None
            poster = f"{TMDB_IMG_BASE}{m['poster_path']}" if m.get('poster_path') else ''
            results.append({
                'type': 'movie',
                'id': m.get('id'),
                'tmdb_id': m.get('id'),
                'imdb_id': '',
                'title': m.get('title') or m.get('original_title') or '',
                'year': year,
                'poster_url': poster,
                'overview': m.get('overview') or '',
            })
        return results
    except Exception as exc:
        logger.warning(f"[Metadata] TMDB movie search failed for '{q}': {exc}")
        return search_movies_imdb(q)


def get_movie_details(tmdb_id: int) -> dict[str, Any] | None:
    """Fetch TMDB movie details including external IMDB ID."""
    api_key = get_tmdb_api_key()
    if not api_key:
        return None
    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{int(tmdb_id)}",
            params={'api_key': api_key, 'append_to_response': 'external_ids'},
            timeout=10,
        )
        resp.raise_for_status()
        m = resp.json()
        rel = m.get('release_date') or ''
        year = int(rel[:4]) if len(rel) >= 4 and rel[:4].isdigit() else None
        ext = m.get('external_ids') or {}
        imdb_id = ext.get('imdb_id') or m.get('imdb_id') or ''
        poster = f"{TMDB_IMG_BASE}{m['poster_path']}" if m.get('poster_path') else ''
        return {
            'type': 'movie',
            'id': m.get('id'),
            'tmdb_id': m.get('id'),
            'imdb_id': imdb_id,
            'title': m.get('title') or m.get('original_title') or '',
            'year': year,
            'poster_url': poster,
            'overview': m.get('overview') or '',
        }
    except Exception as exc:
        logger.warning(f"[Metadata] TMDB movie details failed (id={tmdb_id}): {exc}")
        return None
