"""
integrations/common.py
----------------------
Shared utilities for Rolarr integrations:
  - timestamp formatting
  - title & filename sanitization
  - safe subpath validation for disk operations
  - Plex & Jellyfin credential helpers
"""
from __future__ import annotations

import os
import re
import datetime
import logging

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


def sanitize_filename(name: str) -> str:
    """Sanitize a string so it is safe as a file or directory name on Linux & Windows."""
    if not name:
        return "Unknown"
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(name))
    cleaned = re.sub(r'\s+', ' ', cleaned).strip().rstrip('.')
    return cleaned or "Unknown"


def is_safe_subpath(target_path: str, base_dir: str) -> bool:
    """Return True if target_path resolves strictly inside base_dir (prevents path traversal)."""
    if not target_path or not base_dir:
        return False
    try:
        real_base = os.path.realpath(base_dir)
        real_target = os.path.realpath(target_path)
        if real_target == real_base:
            return False
        return os.path.commonpath([real_base, real_target]) == real_base
    except Exception:
        return False


def get_plex_credentials() -> tuple[str, str]:
    """Return (plex_url, plex_token) from config, with URL trailing-slash stripped."""
    from config_store import get_config
    plex_url = (get_config('PLEX_URL') or '').rstrip('/')
    plex_token = get_config('PLEX_TOKEN') or ''
    return plex_url, plex_token


def get_jellyfin_credentials() -> tuple[str, str]:
    """Return (jellyfin_url, jellyfin_api_key) from config, with URL trailing-slash stripped."""
    from config_store import get_config
    jf_url = (get_config('JELLYFIN_URL') or '').rstrip('/')
    jf_key = get_config('JELLYFIN_API_KEY') or ''
    return jf_url, jf_key


def get_jellyfin_headers(api_key: str) -> dict[str, str]:
    """Return standard API request headers for Jellyfin."""
    return {
        'Authorization': f'MediaBrowser Token="{api_key}"',
        'Accept': 'application/json',
    }
