"""
services/connection_tester.py
-----------------------------
Registry of connection test functions and live status checks for each service:
  - NZBgeek (Newznab)
  - Usenet (NNTP)
  - TMDB (Movie metadata)
  - Plex
  - Jellyfin
  - SSH (Host power saver)
"""
from __future__ import annotations

import logging
import requests

import config_store
from integrations.common import get_plex_credentials, get_jellyfin_credentials, get_jellyfin_headers
from integrations.nzbgeek import get_nzbgeek_url, get_nzbgeek_api_key
from integrations.metadata import get_tmdb_api_key
from services.usenet_downloader import test_nntp_connection

logger = logging.getLogger(__name__)


def _resolve_secret(submitted: str | None, config_key: str) -> str:
    val = submitted or ''
    if not val or val == '••••••••' or str(val).startswith('••••'):
        return config_store.get_config(config_key) or ''
    return val


def _test_nzbgeek(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or get_nzbgeek_url() or '').rstrip('/')
    key = _resolve_secret(data.get('api_key'), 'NZBGEEK_API_KEY')
    if not url or not key:
        return False, 'NZBgeek URL and API Key are required'
    endpoint = f"{url}/api" if not url.endswith('/api') else url
    try:
        res = requests.get(endpoint, params={'t': 'caps', 'apikey': key}, timeout=6)
        if res.status_code == 200 and '<caps' in res.text.lower():
            return True, 'NZBgeek connected successfully!'
        if 'error' in res.text.lower():
            return False, 'NZBgeek rejected API key'
        return False, f'NZBgeek test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'NZBgeek connection error: {exc}'


def _test_usenet(data: dict) -> tuple[bool, str]:
    host = (data.get('host') or config_store.get_config('NNTP_HOST') or '').strip()
    port = int(data.get('port') or config_store.get_config('NNTP_PORT') or 563)
    raw_ssl = data.get('ssl')
    use_ssl = config_store.get_config('NNTP_SSL', True) if raw_ssl is None else (
        raw_ssl if isinstance(raw_ssl, bool) else str(raw_ssl).lower() in ('true', '1', 'yes')
    )
    user = (data.get('username') or config_store.get_config('NNTP_USERNAME') or '').strip()
    pwd = _resolve_secret(data.get('password'), 'NNTP_PASSWORD')
    return test_nntp_connection(host, port, use_ssl, user, pwd)


def _test_tmdb(data: dict) -> tuple[bool, str]:
    key = _resolve_secret(data.get('api_key'), 'TMDB_API_KEY')
    if not key:
        return False, 'TMDB API Key is required for Movie search'
    try:
        res = requests.get('https://api.themoviedb.org/3/configuration', params={'api_key': key}, timeout=6)
        if res.status_code == 200:
            return True, 'TMDB API connected successfully!'
        return False, f'TMDB test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'TMDB connection error: {exc}'


def _test_plex(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or config_store.get_config('PLEX_URL') or '').rstrip('/')
    token = _resolve_secret(data.get('token'), 'PLEX_TOKEN')
    if not url or not token:
        return False, 'Plex URL and Token are required'
    try:
        res = requests.get(f"{url}/identity", headers={'X-Plex-Token': token, 'Accept': 'application/json'}, timeout=4)
        if res.status_code == 200:
            return True, 'Plex connected successfully!'
        return False, f'Plex test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'Plex connection error: {exc}'


def _test_jellyfin(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or config_store.get_config('JELLYFIN_URL') or '').rstrip('/')
    api_key = _resolve_secret(data.get('api_key'), 'JELLYFIN_API_KEY')
    if not url or not api_key:
        return False, 'Jellyfin URL and API Key are required'
    try:
        res = requests.get(f"{url}/System/Info", headers=get_jellyfin_headers(api_key), timeout=4)
        if res.status_code == 200:
            ver = res.json().get('Version', '')
            return True, f'Jellyfin connected successfully! (v{ver})' if ver else 'Jellyfin connected successfully!'
        return False, f'Jellyfin test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'Jellyfin connection error: {exc}'


def _test_ssh(data: dict) -> tuple[bool, str]:
    ssh_host = data.get('ssh_host') or config_store.get_config('SSH_HOST')
    ssh_port = int(data.get('ssh_port') or config_store.get_config('SSH_PORT') or 22)
    ssh_user = data.get('ssh_user') or config_store.get_config('SSH_USER')
    ssh_pass = _resolve_secret(data.get('ssh_password'), 'SSH_PASSWORD')
    if not ssh_host or not ssh_user:
        return False, 'SSH Host and User are required'
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=ssh_host,
            port=ssh_port,
            username=ssh_user,
            password=ssh_pass if ssh_pass else None,
            timeout=5,
        )
        client.close()
        return True, f'SSH connection to {ssh_host}:{ssh_port} successful!'
    except Exception as exc:
        return False, f'SSH connection failed: {exc}'


CONNECTION_TESTERS: dict[str, callable] = {
    'nzbgeek': _test_nzbgeek,
    'usenet': _test_usenet,
    'tmdb': _test_tmdb,
    'plex': _test_plex,
    'jellyfin': _test_jellyfin,
    'ssh': _test_ssh,
}


def test_connection(service: str, data: dict) -> tuple[bool, str]:
    """Test a service connection. Returns (success, message)."""
    tester = CONNECTION_TESTERS.get(service.lower())
    if not tester:
        return False, 'Unknown service'
    return tester(data)


# ── Live connection status checkers ──────────────────────────────────────────

def _check_service_status(url: str, headers: dict, endpoint: str, timeout: int = 2) -> dict:
    if not url:
        return {'status': 'Disconnected', 'ok': False}
    try:
        res = requests.get(f"{url}{endpoint}", headers=headers, timeout=timeout)
        status = "Connected" if res.status_code == 200 else f"Error ({res.status_code})"
    except Exception:
        status = "Unreachable"
    return {'status': status, 'ok': status == 'Connected'}


def check_nzbgeek_status() -> dict:
    url, key = get_nzbgeek_url(), get_nzbgeek_api_key()
    if not url or not key:
        return {'status': 'Disconnected', 'ok': False}
    return {'status': 'Configured', 'ok': True}


def check_usenet_status() -> dict:
    host = (config_store.get_config('NNTP_HOST') or '').strip()
    if not host:
        return {'status': 'Disconnected', 'ok': False}
    return {'status': 'Configured', 'ok': True}


def check_plex_status() -> dict:
    plex_url, plex_token = get_plex_credentials()
    if not plex_url or not plex_token:
        return {'status': 'Disconnected', 'ok': False}
    return _check_service_status(plex_url, {'X-Plex-Token': plex_token, 'Accept': 'application/json'}, '/identity')


def check_jellyfin_status() -> dict:
    jf_url, jf_key = get_jellyfin_credentials()
    if not jf_url or not jf_key:
        return {'status': 'Disconnected', 'ok': False}
    return _check_service_status(jf_url, get_jellyfin_headers(jf_key), '/System/Info')
