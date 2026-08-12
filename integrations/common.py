"""
integrations/common.py
----------------------
Shared utilities and base functions for integrations (Sonarr, Radarr, etc.)
including title sanitization, two-pass title matching, API headers, and timestamp helpers.
"""
from __future__ import annotations

import re
import datetime
import logging
import requests
from typing import Any

logger = logging.getLogger(__name__)


def now_str() -> str:
    """Return current timestamp formatted as 'YYYY-MM-DD HH:MM:SS'."""
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def clean_title(t: str) -> str:
    """Sanitize title by removing year suffix (e.g. '(2023)') and non-alphanumeric characters."""
    if not t:
        return ""
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', t)
    return re.sub(r'[^a-z0-9]', '', t.lower())


def get_arr_headers(api_key: str) -> dict[str, str]:
    """Return standard API request headers for Sonarr / Radarr."""
    if not api_key:
        raise ValueError("API key is not configured")
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json"
    }


def arr_request(
    method: str,
    url: str,
    headers: dict[str, str],
    params: dict | None = None,
    json_data: Any = None,
    timeout: int = 10,
) -> requests.Response:
    """Execute HTTP request against an Arr API with standard timeout and status check."""
    resp = requests.request(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json=json_data,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


def match_media_by_title(
    items: list[dict[str, Any]],
    target_title: str,
    year: int | str | None = None,
    additional_fields: tuple[str, ...] = ('sortTitle', 'cleanTitle', 'originalTitle'),
) -> tuple[int | None, str | None]:
    """
    Perform a robust two-pass match against a list of media items (series or movies).
    Pass 1: Exact string comparison (case-insensitive) against title, alternateTitles, and specified fields.
    Pass 2: Clean alphanumeric comparison ignoring punctuation, spaces, and year suffixes.
    Returns (item_id, item_title) or (None, None).
    """
    if not target_title or not items:
        return None, None

    normalized_target = target_title.lower().strip()
    target_clean = clean_title(target_title)

    def matches_year(item_year: Any) -> bool:
        if year is None or item_year is None:
            return True
        try:
            return int(item_year) == int(year)
        except (ValueError, TypeError):
            return True

    # Pass 1: Exact match
    for item in items:
        if not matches_year(item.get('year')):
            continue

        titles_to_check = [item.get('title', '')]
        for field in additional_fields:
            if field in item and item[field]:
                titles_to_check.append(str(item[field]))

        for t in titles_to_check:
            if t and t.lower().strip() == normalized_target:
                logger.info(f"Resolved '{target_title}' to media ID {item.get('id')} ('{item.get('title')}')")
                return item.get('id'), item.get('title')

        for alt in item.get('alternateTitles', []):
            alt_t = alt.get('title', '') if isinstance(alt, dict) else str(alt)
            if alt_t and alt_t.lower().strip() == normalized_target:
                logger.info(f"Resolved '{target_title}' via alternate title to media ID {item.get('id')}")
                return item.get('id'), item.get('title')

    # Pass 2: Clean alphanumeric match
    if target_clean:
        for item in items:
            if not matches_year(item.get('year')):
                continue

            candidates = [clean_title(item.get('title', ''))]
            for field in additional_fields:
                if field in item and item[field]:
                    candidates.append(clean_title(str(item[field])))

            for alt in item.get('alternateTitles', []):
                alt_t = alt.get('title', '') if isinstance(alt, dict) else str(alt)
                candidates.append(clean_title(alt_t))

            if target_clean in candidates:
                logger.info(f"Resolved '{target_title}' via clean title match to media ID {item.get('id')} ('{item.get('title')}')")
                return item.get('id'), item.get('title')

    return None, None
