"""
services/disk_service.py
------------------------
Monitors available disk space for Downloads, TV Shows, and Movies directories.
Queries Sonarr & Radarr /api/v3/diskspace & /api/v3/rootfolder endpoints for exact
host total, free, and used disk statistics.
"""
from __future__ import annotations

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


def _build_disk_stat(
    label: str,
    configured_path: str,
    mounted_path: str,
    exists: bool,
    total_bytes: int,
    free_bytes: int,
    source: str,
) -> dict:
    """Construct a standardized disk space dictionary across local and remote sources."""
    if exists and total_bytes > 0 and total_bytes >= free_bytes:
        used_bytes = max(0, total_bytes - free_bytes)
        used_pct = round((used_bytes / total_bytes) * 100, 1)
        free_pct = round((free_bytes / total_bytes) * 100, 1)
        status = 'ok' if free_pct >= 20 else ('warning' if free_pct >= 10 else 'critical')
    elif exists:
        used_bytes = 0
        used_pct = 0
        free_pct = 100
        status = 'ok'
    else:
        used_bytes = 0
        used_pct = 0
        free_pct = 0
        status = 'unknown'

    return {
        'label': label,
        'configured_path': configured_path,
        'mounted_path': mounted_path,
        'exists': exists,
        'total_bytes': total_bytes,
        'free_bytes': free_bytes,
        'used_bytes': used_bytes,
        'total_formatted': _format_bytes(total_bytes) if total_bytes > 0 else 'N/A',
        'free_formatted': _format_bytes(free_bytes) if (exists or free_bytes > 0) else 'N/A',
        'used_formatted': _format_bytes(used_bytes) if total_bytes > 0 else 'N/A',
        'used_pct': used_pct,
        'free_pct': free_pct,
        'status': status,
        'source': source,
    }


def _check_local_disk(path_key: str, default_path: str, label: str) -> dict:
    configured_path = get_config(path_key, default_path) or default_path
    target_path = configured_path if os.path.exists(configured_path) else ('C:\\' if os.name == 'nt' else '/')
    
    try:
        usage = shutil.disk_usage(target_path)
        return _build_disk_stat(
            label=label,
            configured_path=configured_path,
            mounted_path=target_path,
            exists=os.path.exists(configured_path),
            total_bytes=usage.total,
            free_bytes=usage.free,
            source='Local Container Mount',
        )
    except Exception as exc:
        logger.warning(f"[DiskService] Error checking path {configured_path}: {exc}")
        return _build_disk_stat(
            label=label,
            configured_path=configured_path,
            mounted_path=target_path,
            exists=False,
            total_bytes=0,
            free_bytes=0,
            source='Local Container Mount',
        )


def _fetch_arr_diskspace(url: str, headers: dict, target_root_path: str) -> tuple[int, int]:
    """Query Sonarr/Radarr /api/v3/diskspace endpoint for matching mount freeSpace & totalSpace."""
    try:
        res = requests.get(f"{url.rstrip('/')}/api/v3/diskspace", headers=headers, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) > 0:
                best_match = None
                best_len = -1
                for disk in data:
                    disk_path = disk.get('path', '')
                    if disk_path and (target_root_path.startswith(disk_path) or disk_path.startswith(target_root_path)):
                        if len(disk_path) > best_len:
                            best_len = len(disk_path)
                            best_match = disk
                if not best_match:
                    best_match = data[0]
                
                if best_match:
                    free_b = best_match.get('freeSpace', 0)
                    total_b = best_match.get('totalSpace', 0)
                    return free_b, total_b
    except Exception as exc:
        logger.debug(f"[DiskService] Failed fetching diskspace API: {exc}")
    return 0, 0


def _check_arr_root_folder(service_name: str, url_fn, key_fn, headers_fn, label: str, fallback_key: str, fallback_path: str) -> dict:
    """Fetch root folder path and disk space statistics directly from Sonarr or Radarr API."""
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
                    rf_free = rf.get('freeSpace', 0)
                    rf_total = rf.get('totalSpace', 0)
                    
                    ds_free, ds_total = _fetch_arr_diskspace(url, headers, path)
                    free_b = ds_free or rf_free
                    total_b = ds_total or rf_total
                    
                    if not total_b or total_b <= 0:
                        if os.path.exists(path):
                            try:
                                local_usage = shutil.disk_usage(path)
                                total_b = local_usage.total
                            except Exception:
                                pass
                    
                    return _build_disk_stat(
                        label=label,
                        configured_path=path,
                        mounted_path=path,
                        exists=True,
                        total_bytes=total_b,
                        free_bytes=free_b,
                        source=f"{service_name} Host Path",
                    )
    except Exception as exc:
        logger.debug(f"[DiskService] Failed fetching {service_name} root folder: {exc}")
    
    return _check_local_disk(fallback_key, fallback_path, label)


def get_disk_space_summary() -> dict:
    return {
        'disks': {
            'downloads': _check_local_disk('PATH_DOWNLOADS', '/downloads', 'Downloads Directory'),
            'tv':        _check_arr_root_folder('Sonarr', get_sonarr_url, get_sonarr_api_key, get_sonarr_headers, 'TV Shows Directory', 'PATH_TV', '/tv'),
            'movies':    _check_arr_root_folder('Radarr', get_radarr_url, get_radarr_api_key, get_radarr_headers, 'Movies Directory', 'PATH_MOVIES', '/movies'),
        }
    }
