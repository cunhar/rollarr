from __future__ import annotations

import os
import requests
import logging

logger = logging.getLogger(__name__)

NZBGET_URL = os.environ.get('NZBGET_URL', '').rstrip('/')
NZBGET_USERNAME = os.environ.get('NZBGET_USERNAME', '')
NZBGET_PASSWORD = os.environ.get('NZBGET_PASSWORD', '')

def get_auth():
    if NZBGET_USERNAME or NZBGET_PASSWORD:
        return (NZBGET_USERNAME, NZBGET_PASSWORD)
    return None

def nzbget_rpc(method: str, params: list = None) -> dict | list | None:
    if not NZBGET_URL:
        return None
    url = f"{NZBGET_URL}/jsonrpc"
    payload = {
        "method": method,
        "params": params or []
    }
    try:
        resp = requests.post(url, json=payload, auth=get_auth(), timeout=5)
        resp.raise_for_status()
        res_json = resp.json()
        return res_json.get('result')
    except Exception as exc:
        logger.warning(f"[NZBGet] JSON-RPC request failed ({method}): {exc}")
        return None

def get_nzbget_status() -> dict:
    """
    Query NZBGet status and listgroups.
    Returns structured status dictionary.
    """
    if not NZBGET_URL:
        return {'enabled': False, 'connected': False, 'status_text': 'Not Configured', 'downloads': []}

    status = nzbget_rpc('status')
    if status is None:
        return {'enabled': True, 'connected': False, 'status_text': 'Unreachable', 'downloads': []}

    download_rate_bps = int(status.get('DownloadRate', 0))
    if download_rate_bps >= 1024 * 1024:
        rate_str = f"{download_rate_bps / (1024 * 1024):.1f} MB/s"
    elif download_rate_bps >= 1024:
        rate_str = f"{download_rate_bps / 1024:.0f} KB/s"
    else:
        rate_str = f"{download_rate_bps} B/s"

    groups = nzbget_rpc('listgroups') or []
    downloads = []
    for g in groups:
        name = g.get('NZBName', 'Unknown NZB')
        size_mb = float(g.get('FileSizeMB', 0))
        rem_mb = float(g.get('RemainingSizeMB', 0))
        done_mb = max(0, size_mb - rem_mb)
        pct = round((done_mb / size_mb * 100), 1) if size_mb > 0 else 0
        
        # Calculate item speed & ETA if downloading
        status_str = g.get('Status', 'QUEUED').upper()
        
        # Determine remaining time
        if download_rate_bps > 0 and rem_mb > 0:
            rem_bytes = rem_mb * 1024 * 1024
            eta_seconds = int(rem_bytes / download_rate_bps)
            eta_mins = round(eta_seconds / 60)
            eta_str = f"{eta_mins} min left" if eta_mins > 0 else "< 1 min left"
        else:
            eta_str = "—"
            
        downloads.append({
            'name': name,
            'size_mb': round(size_mb, 1),
            'remaining_mb': round(rem_mb, 1),
            'progress_pct': pct,
            'status': status_str,
            'eta': eta_str,
            'category': g.get('Category', ''),
        })

    return {
        'enabled': True,
        'connected': True,
        'status_text': 'Connected',
        'download_rate': rate_str,
        'download_rate_bps': download_rate_bps,
        'download_paused': status.get('DownloadPaused', False),
        'downloads': downloads,
    }
