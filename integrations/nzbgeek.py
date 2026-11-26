"""
integrations/nzbgeek.py
-----------------------
Newznab API client tailored for NZBgeek (works with any standard Newznab endpoint).
Parses XML search results using Python's stdlib xml.etree.ElementTree.
"""
from __future__ import annotations

import re
import logging
import xml.etree.ElementTree as ET
from typing import Any

import requests

from config_store import get_config

logger = logging.getLogger(__name__)

NEWZNAB_NS = '{http://www.newznab.com/DTD/2010/feeds/attributes/}'


def get_nzbgeek_url() -> str:
    return (get_config('NZBGEEK_URL') or 'https://api.nzbgeek.info').rstrip('/')


def get_nzbgeek_api_key() -> str:
    return (get_config('NZBGEEK_API_KEY') or '').strip()


def _parse_newznab_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse Newznab RSS XML into a list of normalized release candidate dicts."""
    items: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning(f"[NZBgeek] Failed parsing Newznab XML: {exc}")
        return items

    for item in root.findall('.//item'):
        title = (item.findtext('title') or '').strip()
        link = (item.findtext('link') or '').strip()
        guid = (item.findtext('guid') or link or title).strip()
        pub_date = (item.findtext('pubDate') or '').strip()

        enclosure = item.find('enclosure')
        size_bytes = 0
        if enclosure is not None:
            if not link and enclosure.attrib.get('url'):
                link = enclosure.attrib['url'].strip()
            try:
                size_bytes = int(enclosure.attrib.get('length') or 0)
            except (ValueError, TypeError):
                size_bytes = 0

        attrs: dict[str, str] = {}
        for attr in item.findall(f'{NEWZNAB_NS}attr'):
            name = (attr.attrib.get('name') or '').lower()
            val = attr.attrib.get('value') or ''
            if name:
                attrs[name] = val

        # Also handle non-namespaced <attr> fallback
        if not attrs:
            for child in item:
                if child.tag.endswith('attr'):
                    name = (child.attrib.get('name') or '').lower()
                    val = child.attrib.get('value') or ''
                    if name:
                        attrs[name] = val

        if not size_bytes and 'size' in attrs:
            try:
                size_bytes = int(attrs['size'])
            except (ValueError, TypeError):
                size_bytes = 0

        try:
            grabs = int(attrs.get('grabs') or 0)
        except (ValueError, TypeError):
            grabs = 0

        passworded = attrs.get('password', '0') in ('1', 'true', 'yes')

        if title and link:
            items.append({
                'title': title,
                'nzb_url': link,
                'guid': guid,
                'size_bytes': size_bytes,
                'grabs': grabs,
                'passworded': passworded,
                'pub_date': pub_date,
                'attrs': attrs,
            })

    return items


def _query(params: dict[str, Any]) -> list[dict[str, Any]]:
    url = get_nzbgeek_url()
    api_key = get_nzbgeek_api_key()
    if not url or not api_key:
        logger.warning("[NZBgeek] URL or API key not configured")
        return []

    endpoint = f"{url}/api" if not url.endswith('/api') else url
    full_params = {'apikey': api_key, 'limit': 100, **params}
    try:
        resp = requests.get(endpoint, params=full_params, timeout=15)
        resp.raise_for_status()
        return _parse_newznab_xml(resp.text)
    except Exception as exc:
        logger.warning(f"[NZBgeek] Search request failed ({params.get('t')}): {exc}")
        return []


def search_episode(
    show_title: str,
    season: int,
    episode: int,
    tvdb_id: int | None = None,
    imdb_id: str | None = None,
    airdate: str | None = None,
    include_season_packs: bool = False,
) -> list[dict[str, Any]]:
    """
    Search NZBgeek for a specific TV episode.
    Supports Standard (SxxExx), Sonarr Daily/Date-based (YYYY.MM.DD / season=YYYY&ep=MM/DD),
    and optional Season Pack fallback queries.
    """
    results: list[dict[str, Any]] = []
    seen_guids: set[str] = set()

    def _add_unique(items: list[dict[str, Any]]):
        for it in items:
            g = it.get('guid') or it.get('nzb_url') or it.get('title')
            if g and g not in seen_guids:
                seen_guids.add(g)
                results.append(it)

    clean_imdb = re.sub(r'^tt', '', str(imdb_id or '')).strip()
    has_imdb = clean_imdb.isdigit()

    # 1. Standard SxxExx Newznab tvsearch (prefer exact TVDB ID, else IMDb ID, then title tvsearch)
    if tvdb_id:
        _add_unique(_query({
            't': 'tvsearch',
            'tvdbid': int(tvdb_id),
            'season': int(season),
            'ep': int(episode),
        }))
    elif has_imdb:
        _add_unique(_query({
            't': 'tvsearch',
            'imdbid': clean_imdb,
            'season': int(season),
            'ep': int(episode),
        }))
    if not results:
        _add_unique(_query({
            't': 'tvsearch',
            'q': show_title,
            'season': int(season),
            'ep': int(episode),
        }))

    # 2. Sonarr Daily / Airdate Newznab search (season=YYYY, ep=MM/DD and text YYYY MM DD)
    ad_match = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', str(airdate or '').strip())
    if not results and ad_match:
        yyyy, mm, dd = ad_match.group(1), ad_match.group(2), ad_match.group(3)
        ep_date = f"{mm}/{dd}"
        if tvdb_id:
            _add_unique(_query({
                't': 'tvsearch',
                'tvdbid': int(tvdb_id),
                'season': yyyy,
                'ep': ep_date,
            }))
        elif has_imdb:
            _add_unique(_query({
                't': 'tvsearch',
                'imdbid': clean_imdb,
                'season': yyyy,
                'ep': ep_date,
            }))
        if not results:
            _add_unique(_query({
                't': 'search',
                'q': f"{show_title} {yyyy} {mm} {dd}",
            }))

    # 3. Final free-text SxxExx search fallback (only when no tvdb_id/airdate matched)
    if not results and not tvdb_id:
        ep_tag = f"S{int(season):02d}E{int(episode):02d}"
        _add_unique(_query({
            't': 'search',
            'q': f"{show_title} {ep_tag}",
        }))

    # 4. Season pack search (when explicitly requested or when no single-episode release exists)
    if include_season_packs or not results:
        if tvdb_id:
            _add_unique(_query({
                't': 'tvsearch',
                'tvdbid': int(tvdb_id),
                'season': int(season),
            }))
        else:
            _add_unique(_query({
                't': 'tvsearch',
                'q': show_title,
                'season': int(season),
            }))

    logger.info(
        f"[NZBgeek] Episode search '{show_title}' S{season:02d}E{episode:02d} "
        f"(airdate={airdate or 'none'}) -> {len(results)} candidate(s)"
    )
    return results


def search_movie(
    title: str,
    year: int | None = None,
    imdb_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Search NZBgeek for a movie.
    Queries by IMDB ID first (exact, like Radarr), and also falls back to
    Newznab movie category text search (t=search&cat=2000) and t=movie&q=...
    """
    results: list[dict[str, Any]] = []
    seen_guids: set[str] = set()

    def _add_unique(items: list[dict[str, Any]], matched_imdb: bool = False):
        for it in items:
            g = it.get('guid') or it.get('nzb_url') or it.get('title')
            if g and g not in seen_guids:
                seen_guids.add(g)
                if matched_imdb:
                    it['matched_by_imdb'] = True
                results.append(it)

    if imdb_id:
        clean_imdb = re.sub(r'^tt', '', str(imdb_id).strip())
        if clean_imdb.isdigit():
            _add_unique(_query({
                't': 'movie',
                'imdbid': clean_imdb,
            }), matched_imdb=True)

    q_with_year = f"{title} {year}".strip() if year else title.strip()
    if not results and q_with_year:
        _add_unique(_query({
            't': 'search',
            'cat': '2000',
            'q': q_with_year,
        }))
    if not results and title:
        _add_unique(_query({
            't': 'movie',
            'q': title.strip(),
        }))
    if not results and q_with_year:
        _add_unique(_query({
            't': 'search',
            'q': q_with_year,
        }))

    logger.info(f"[NZBgeek] Movie search '{title}' ({year or ''}, imdb={imdb_id or 'none'}) -> {len(results)} candidate(s)")
    return results


def download_nzb_bytes(nzb_url: str) -> bytes:
    """Fetch the raw .nzb XML payload from NZBgeek."""
    resp = requests.get(nzb_url, timeout=20)
    resp.raise_for_status()
    return resp.content
