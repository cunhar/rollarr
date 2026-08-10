"""
services/disk_service.py
------------------------
Monitors available disk space for Downloads, TV Shows, and Movies directories.
Queries Sonarr & Radarr root folder APIs directly for TV and Movies host disk space,
falling back to local shutil.disk_usage() if services are unconfigured/unreachable.
"""

import shutil
import os
import requests
import logging
from config_store import get_config
from integrations.sonarr import get_sonarr_url, get_sonarr_api_key, get_sonarr_headers
from integrations.radarr import get_radarr_url, get_radarr_api_key, get_radarr_headers

logger = logging.getLogger(__name__)


def _format_bytes(bytes_val: int) -> str:
    if bytes_val is None or bytes_val < 0:
        return 'Unknown'
    gb = bytes_val / (1024 ** 3)
    if gb >= 1000:
        tb = gb / 1024
        return f"{tb:.2f} TB"
    return f"{gb:.1f} GB"


def _check_local_disk(path_key: str, default_path: str, label: str) -> dict:
    configured_path = get_config(path_key, default_path) or default_path
    
    # Try configured path, fallback to root if path doesn't exist locally inside container
    target_path = configured_path if os.path.exists(configured_path) else ('C:\\' if os.name == 'nt' else '/')
    
    try:
        usage = shutil.disk_usage(target_path)
        total = usage.total
        free = usage.free
        used = usage.used
        used_pct = round((used / total) * 100, 1) if total > 0 else 0
        free_pct = round((free / total) * 100, 1) if total > 0 else 0
        
        status = 'ok' if free_pct >= 20 else ('warning' if free_pct >= 10 else 'critical')
        
        return {
            'label': label,
            'configured_path': configured_path,
            'mounted_path': target_path,
            'exists': os.path.exists(configured_path),
            'total_bytes': total,
            'free_bytes': free,
            'used_bytes': used,
            'total_formatted': _format_bytes(total),
            'free_formatted': _format_bytes(free),
            'used_formatted': _format_bytes(used),
            'used_pct': used_pct,
            'free_pct': free_pct,
            'status': status,
            'source': 'Local Container Mount',
        }
    except Exception as exc:
        logger.warning(f"[DiskService] Error checking path {configured_path}: {exc}")
        return {
            'label': label,
            'configured_path': configured_path,
            'mounted_path': target_path,
            'exists': False,
            'total_formatted': 'N/A',
            'free_formatted': 'N/A',
            'used_formatted': 'N/A',
            'used_pct': 0,
            'free_pct': 0,
            'status': 'unknown',
            'source': 'Local Container Mount',
        }


def _check_arr_root_folder(service_name: str, url_fn, key_fn, headers_fn, label: str, fallback_key: str, fallback_path: str) -> dict:
    """Fetch root folder disk space statistics directly from Sonarr or Radarr API."""
    try:
        url = url_fn()
        key = key_fn()
        if url and key:
            headers = headers_fn()
            res = requests.get(f"{url.rstrip('/')}/api/v3/rootfolder", headers=headers, timeout=3)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and len(data) > 0:
                    rf = data[0]
                    path = rf.get('path', '')
                    free_b = rf.get('freeSpace', 0)
                    total_b = rf.get('totalSpace', 0)
                    
                    if total_b and total_b > 0:
                        used_b = max(0, total_b - free_b)
                        used_pct = round((used_b / total_b) * 100, 1)
                        free_pct = round((free_b / total_b) * 100, 1)
                        status = 'ok' if free_pct >= 20 else ('warning' if free_pct >= 10 else 'critical')
                    else:
                        used_b = 0
                        used_pct = 0
                        free_pct = 100
                        status = 'ok'
                    
                    return {
                        'label': label,
                        'configured_path': path,
                        'mounted_path': path,
                        'exists': True,
                        'total_bytes': total_b,
                        'free_bytes': free_b,
                        'used_bytes': used_b,
                        'total_formatted': _format_bytes(total_b) if total_b > 0 else 'N/A',
                        'free_formatted': _format_bytes(free_b),
                        'used_formatted': _format_bytes(used_b) if total_b > 0 else 'N/A',
                        'used_pct': used_pct,
                        'free_pct': free_pct,
                        'status': status,
                        'source': f"{service_name} Host Path",
                    }
    except Exception as exc:
        logger.debug(f"[DiskService] Failed fetching {service_name} root folder: {exc}")
    
    # Fallback to local container disk check if Arr service API unavailable
    return _check_local_disk(fallback_key, fallback_path, label)


def get_disk_space_summary() -> dict:
    disks = {
        'downloads': _check_local_disk('PATH_DOWNLOADS', '/downloads', 'Downloads Directory'),
        'tv':        _check_arr_root_folder('Sonarr', get_sonarr_url, get_sonarr_api_key, get_sonarr_headers, 'TV Shows Directory', 'PATH_TV', '/tv'),
        'movies':    _check_arr_root_folder('Radarr', get_radarr_url, get_radarr_api_key, get_radarr_headers, 'Movies Directory', 'PATH_MOVIES', '/movies'),
    }

    return {
        'disks': disks,
    }
