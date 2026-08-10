"""
services/disk_service.py
------------------------
Monitors available disk space for Downloads, TV Shows, and Movies directories.
Combines local shutil.disk_usage() with Sonarr & Radarr root folder API statistics.
"""

import shutil
import os
import requests
import logging
from config_store import get_config
from integrations.sonarr import get_sonarr_url, get_sonarr_headers
from integrations.radarr import get_radarr_url, get_radarr_headers

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
    
    # Try configured path, fallback to '/' if path doesn't exist locally
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
        }


def get_disk_space_summary() -> dict:
    disks = {
        'downloads': _check_local_disk('PATH_DOWNLOADS', '/downloads', 'Downloads Directory'),
        'tv':        _check_local_disk('PATH_TV',        '/tv',        'TV Shows Directory'),
        'movies':    _check_local_disk('PATH_MOVIES',    '/movies',    'Movies Directory'),
    }

    # Fetch Sonarr root folders
    sonarr_folders = []
    sonarr_url = get_sonarr_url()
    if sonarr_url:
        try:
            res = requests.get(f"{sonarr_url}/api/v3/rootfolder", headers=get_sonarr_headers(), timeout=3)
            if res.status_code == 200:
                for rf in res.json():
                    free_b = rf.get('freeSpace', 0)
                    sonarr_folders.append({
                        'path': rf.get('path', ''),
                        'free_formatted': _format_bytes(free_b),
                    })
        except Exception:
            pass

    # Fetch Radarr root folders
    radarr_folders = []
    radarr_url = get_radarr_url()
    if radarr_url:
        try:
            res = requests.get(f"{radarr_url}/api/v3/rootfolder", headers=get_radarr_headers(), timeout=3)
            if res.status_code == 200:
                for rf in res.json():
                    free_b = rf.get('freeSpace', 0)
                    radarr_folders.append({
                        'path': rf.get('path', ''),
                        'free_formatted': _format_bytes(free_b),
                    })
        except Exception:
            pass

    return {
        'disks': disks,
        'sonarr_root_folders': sonarr_folders,
        'radarr_root_folders': radarr_folders,
    }
