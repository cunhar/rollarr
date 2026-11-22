"""
services/disk_service.py
------------------------
Monitors available disk space for Downloads, TV Shows, and Movies directories
directly from the container's mounted paths.
"""
from __future__ import annotations

import shutil
import os
import logging
from config_store import get_config

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
    if total_bytes > 0 and total_bytes >= free_bytes:
        used_bytes = max(0, total_bytes - free_bytes)
        used_pct = round((used_bytes / total_bytes) * 100, 1)
        free_pct = round((free_bytes / total_bytes) * 100, 1)
        status = 'ok' if free_pct >= 20 else ('warning' if free_pct >= 10 else 'critical')
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
        'free_formatted': _format_bytes(free_bytes) if total_bytes > 0 else 'N/A',
        'used_formatted': _format_bytes(used_bytes) if total_bytes > 0 else 'N/A',
        'used_pct': used_pct,
        'free_pct': free_pct,
        'status': status,
        'source': source,
    }


def _check_local_disk(path_key: str, default_path: str, label: str) -> dict:
    configured_path = get_config(path_key, default_path) or default_path
    exists = os.path.exists(configured_path)
    target_path = configured_path if exists else ('C:\\' if os.name == 'nt' else '/')

    try:
        usage = shutil.disk_usage(target_path)
        return _build_disk_stat(
            label=label,
            configured_path=configured_path,
            mounted_path=target_path,
            exists=exists,
            total_bytes=usage.total,
            free_bytes=usage.free,
            source='Local Storage',
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
            source='Local Storage',
        )


def get_disk_space_summary() -> dict:
    return {
        'disks': {
            'downloads': _check_local_disk('PATH_DOWNLOADS', '/downloads', 'Downloads Directory'),
            'tv':        _check_local_disk('PATH_TV', '/tv', 'TV Shows Directory'),
            'movies':    _check_local_disk('PATH_MOVIES', '/movies', 'Movies Directory'),
        }
    }


def check_disk_space_guard() -> tuple[bool, str]:
    """
    Return (is_low: bool, reason: str) if any mounted media/downloads path has
    less free space than MIN_FREE_DISK_GB (0 disables guard).
    """
    min_gb = float(get_config('MIN_FREE_DISK_GB', 15) or 0)
    if min_gb <= 0:
        return False, ''
    min_bytes = min_gb * (1024 ** 3)
    summary = get_disk_space_summary()['disks']
    for key, d in summary.items():
        if d.get('exists') and d.get('total_bytes', 0) > 0 and d.get('free_bytes', 0) < min_bytes:
            return True, f"Low disk space on {d['label']} ({d['free_formatted']} free < {min_gb:.0f} GB guard)"
    return False, ''
