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
from integrations.common import clean_title
from services import db

logger = logging.getLogger(__name__)


def _format_bytes(bytes_val: int) -> str:
    if bytes_val is None or bytes_val < 0:
        return 'Unknown'
    gb = bytes_val / (1024 ** 3)
    if gb >= 1000:
        tb = gb / 1024
        return f"{tb:.2f} TB"
    if gb < 1.0 and bytes_val > 0:
        mb = bytes_val / (1024 ** 2)
        return f"{mb:.0f} MB"
    return f"{gb:.1f} GB"


def _get_dir_size_bytes(path: str) -> int:
    """Fast recursive directory size calculation in bytes."""
    if not path or not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _build_disk_stat(
    label: str,
    configured_path: str,
    mounted_path: str,
    exists: bool,
    total_bytes: int,
    free_bytes: int,
    source: str,
    folder_bytes: int = 0,
) -> dict:
    if total_bytes > 0 and total_bytes >= free_bytes:
        used_bytes = max(0, total_bytes - free_bytes)
        used_pct = round((used_bytes / total_bytes) * 100, 1)
        free_pct = round((free_bytes / total_bytes) * 100, 1)
        folder_pct = round((folder_bytes / total_bytes) * 100, 1)
        status = 'ok' if free_pct >= 20 else ('warning' if free_pct >= 10 else 'critical')
    else:
        used_bytes = 0
        used_pct = 0
        free_pct = 0
        folder_pct = 0
        status = 'unknown'

    return {
        'label': label,
        'configured_path': configured_path,
        'mounted_path': mounted_path,
        'exists': exists,
        'total_bytes': total_bytes,
        'free_bytes': free_bytes,
        'used_bytes': used_bytes,
        'folder_bytes': folder_bytes,
        'total_formatted': _format_bytes(total_bytes) if total_bytes > 0 else 'N/A',
        'free_formatted': _format_bytes(free_bytes) if total_bytes > 0 else 'N/A',
        'used_formatted': _format_bytes(used_bytes) if total_bytes > 0 else 'N/A',
        'folder_formatted': _format_bytes(folder_bytes),
        'used_pct': used_pct,
        'free_pct': free_pct,
        'folder_pct': folder_pct,
        'status': status,
        'source': source,
    }


def _resolve_block_device(target_path: str) -> str:
    """Resolve the underlying block device (e.g. '/dev/sda2') from /proc/mounts when available."""
    if not target_path:
        return ''
    try:
        real_target = os.path.realpath(target_path)
        if os.path.isfile('/proc/mounts'):
            best_mount = ''
            best_dev = ''
            with open('/proc/mounts', 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    dev, mnt = parts[0], parts[1]
                    if not dev.startswith('/dev/'):
                        continue
                    if real_target == mnt or real_target.startswith(mnt.rstrip('/') + '/') or mnt == '/':
                        if len(mnt) >= len(best_mount):
                            best_mount = mnt
                            best_dev = dev
            if best_dev:
                return best_dev
    except Exception:
        pass
    if os.name == 'nt':
        drive, _ = os.path.splitdrive(os.path.abspath(target_path))
        if drive:
            return drive.upper()
    return ''


def _check_local_disk(path_key: str, default_path: str, label: str) -> dict:
    configured_path = get_config(path_key, default_path) or default_path
    exists = os.path.exists(configured_path)
    target_path = configured_path if exists else ('C:\\' if os.name == 'nt' else '/')
    folder_bytes = _get_dir_size_bytes(configured_path) if exists else 0
    device = _resolve_block_device(target_path)
    try:
        dev_id = os.stat(target_path).st_dev
    except OSError:
        dev_id = None

    try:
        usage = shutil.disk_usage(target_path)
        stat = _build_disk_stat(
            label=label,
            configured_path=configured_path,
            mounted_path=target_path,
            exists=exists,
            total_bytes=usage.total,
            free_bytes=usage.free,
            source='Local Storage',
            folder_bytes=folder_bytes,
        )
        stat['device'] = device
        stat['dev_id'] = dev_id
        return stat
    except Exception as exc:
        logger.warning(f"[DiskService] Error checking path {configured_path}: {exc}")
        stat = _build_disk_stat(
            label=label,
            configured_path=configured_path,
            mounted_path=target_path,
            exists=False,
            total_bytes=0,
            free_bytes=0,
            source='Local Storage',
            folder_bytes=0,
        )
        stat['device'] = device
        stat['dev_id'] = dev_id
        return stat


def _get_largest_media_items(total_disk_bytes: int = 0) -> list[dict]:
    """Return a sorted list of on-disk movies and TV shows with their folder sizes and DB IDs."""
    items: list[dict] = []
    movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
    tv_root = get_config('PATH_TV', '/tv') or '/tv'

    try:
        from services import media_manager
        movies = media_manager.sync_on_disk_movies()
        shows = media_manager.sync_on_disk_shows()
    except Exception:
        movies = db.list_movies()
        shows = db.list_shows()

    movies_by_clean = {clean_title(m['title']): m for m in movies if m.get('title')}
    shows_by_clean = {clean_title(s['title']): s for s in shows if s.get('title')}

    if os.path.isdir(movies_root):
        for entry in os.listdir(movies_root):
            full = os.path.join(movies_root, entry)
            sz = _get_dir_size_bytes(full)
            if sz <= 0:
                continue
            ec = clean_title(entry)
            matched = movies_by_clean.get(ec)
            if not matched:
                for mc, m_obj in movies_by_clean.items():
                    if mc and len(mc) >= 3 and (mc in ec or ec in mc):
                        matched = m_obj
                        break
            items.append({
                'type': 'movie',
                'id': matched['id'] if matched else None,
                'title': matched['title'] if matched else entry,
                'year': matched.get('year') if matched else None,
                'folder_name': entry,
                'size_bytes': sz,
                'size_formatted': _format_bytes(sz),
                'pct_of_disk': round((sz / total_disk_bytes) * 100, 1) if total_disk_bytes > 0 else 0,
                'detail': 'Movie',
            })

    if os.path.isdir(tv_root):
        for entry in os.listdir(tv_root):
            full = os.path.join(tv_root, entry)
            if not os.path.isdir(full):
                continue
            sz = _get_dir_size_bytes(full)
            if sz <= 0:
                continue
            ec = clean_title(entry)
            matched = shows_by_clean.get(ec)
            ep_cnt = 0
            try:
                from services import media_manager
                ep_cnt = len(media_manager._scan_show_episodes_on_disk(matched['title'] if matched else entry))
            except Exception:
                pass
            items.append({
                'type': 'tv',
                'id': matched['id'] if matched else None,
                'title': matched['title'] if matched else entry,
                'year': matched.get('year') if matched else None,
                'folder_name': entry,
                'size_bytes': sz,
                'size_formatted': _format_bytes(sz),
                'pct_of_disk': round((sz / total_disk_bytes) * 100, 1) if total_disk_bytes > 0 else 0,
                'detail': f"TV Show • {ep_cnt} ep{'s' if ep_cnt != 1 else ''} on disk" if ep_cnt else 'TV Show',
            })

    items.sort(key=lambda x: x['size_bytes'], reverse=True)
    return items


def _build_physical_disks(disks: dict[str, dict]) -> list[dict]:
    """Deduplicate folder mounts into physical machine disks for the top-level storage bar."""
    grouped: dict[Any, dict] = {}
    short_names = {'downloads': 'Downloads', 'tv': 'TV Shows', 'movies': 'Movies'}

    for key, d in disks.items():
        total_b = int(d.get('total_bytes') or 0)
        if total_b <= 0:
            continue
        dev_name = d.get('device') or ''
        dev_key = dev_name or d.get('dev_id') or (total_b, int(d.get('free_bytes') or 0))
        if dev_key not in grouped:
            label = f"Machine Disk ({dev_name})" if dev_name else "Machine Disk"
            grouped[dev_key] = {
                'device': dev_name or 'Local Disk',
                'label': label,
                'mounts': [],
                'total_bytes': total_b,
                'used_bytes': int(d.get('used_bytes') or 0),
                'free_bytes': int(d.get('free_bytes') or 0),
                'total_formatted': d.get('total_formatted', 'N/A'),
                'used_formatted': d.get('used_formatted', 'N/A'),
                'free_formatted': d.get('free_formatted', 'N/A'),
                'used_pct': d.get('used_pct', 0),
                'free_pct': d.get('free_pct', 0),
                'status': d.get('status', 'ok'),
                'tracked_folder_bytes': 0,
            }
        grouped[dev_key]['mounts'].append(short_names.get(key, d.get('label', key)))
        grouped[dev_key]['tracked_folder_bytes'] += int(d.get('folder_bytes') or 0)

    out = []
    for g in grouped.values():
        total_b = g['total_bytes']
        tracked_b = min(g['used_bytes'], g['tracked_folder_bytes'])
        g['tracked_formatted'] = _format_bytes(tracked_b)
        g['tracked_pct'] = round((tracked_b / total_b) * 100, 1) if total_b > 0 else 0
        out.append(g)
    return out


def get_disk_space_summary() -> dict:
    disks = {
        'downloads': _check_local_disk('PATH_DOWNLOADS', '/downloads', 'Downloads Directory'),
        'tv':        _check_local_disk('PATH_TV', '/tv', 'TV Shows Directory'),
        'movies':    _check_local_disk('PATH_MOVIES', '/movies', 'Movies Directory'),
    }
    ref_total = (disks['movies'].get('total_bytes') or disks['tv'].get('total_bytes') or 0)
    return {
        'physical_disks': _build_physical_disks(disks),
        'disks': disks,
        'largest_items': _get_largest_media_items(ref_total),
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
