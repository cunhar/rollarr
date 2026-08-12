"""
services/connection_tester.py
-----------------------------
Registry of connection test functions for each service.
Each tester returns (success: bool, message: str).

Extracted from app.py to keep route handler clean.
"""
from __future__ import annotations

import logging
import requests

import config_store
from config_store import get_config
from integrations.common import get_plex_credentials
from integrations.sonarr import get_sonarr_url, get_sonarr_api_key
from integrations.radarr import get_radarr_url, get_radarr_api_key
from integrations.nzbget import get_nzbget_url, get_nzbget_username, get_nzbget_password

logger = logging.getLogger(__name__)


def _test_sonarr(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or get_sonarr_url() or '').rstrip('/')
    key = data.get('api_key') or ''
    if not key or key == '••••••••':
        key = get_sonarr_api_key()
    if not url or not key:
        return False, 'Sonarr URL and API Key are required'
    try:
        res = requests.get(f"{url}/api/v3/system/status", headers={'X-Api-Key': key}, timeout=4)
        if res.status_code == 200:
            ver = res.json().get('version', '')
            return True, f'Sonarr connected successfully! (v{ver})'
        return False, f'Sonarr test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'Sonarr connection error: {exc}'


def _test_radarr(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or get_radarr_url() or '').rstrip('/')
    key = data.get('api_key') or ''
    if not key or key == '••••••••':
        key = get_radarr_api_key()
    if not url or not key:
        return False, 'Radarr URL and API Key are required'
    try:
        res = requests.get(f"{url}/api/v3/system/status", headers={'X-Api-Key': key}, timeout=4)
        if res.status_code == 200:
            ver = res.json().get('version', '')
            return True, f'Radarr connected successfully! (v{ver})'
        return False, f'Radarr test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'Radarr connection error: {exc}'


def _test_plex(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or config_store.get_config('PLEX_URL') or '').rstrip('/')
    token = data.get('token') or ''
    if not token or token == '••••••••':
        token = config_store.get_config('PLEX_TOKEN') or ''
    if not url or not token:
        return False, 'Plex URL and Token are required'
    try:
        res = requests.get(f"{url}/identity", headers={'X-Plex-Token': token, 'Accept': 'application/json'}, timeout=4)
        if res.status_code == 200:
            return True, 'Plex connected successfully!'
        return False, f'Plex test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'Plex connection error: {exc}'


def _test_nzbget(data: dict) -> tuple[bool, str]:
    url = (data.get('url') or get_nzbget_url() or '').rstrip('/')
    user = data.get('username') or get_nzbget_username() or 'nzbget'
    password = data.get('password') or ''
    if not password or password == '••••••••':
        password = get_nzbget_password()
    if not url:
        return False, 'NZBGet URL is required'
    try:
        auth = (user, password) if (user or password) else None
        res = requests.get(f"{url}/jsonrpc/version", auth=auth, timeout=4)
        if res.status_code == 200:
            ver = res.json().get('result', '')
            return True, f'NZBGet connected successfully! (v{ver})'
        return False, f'NZBGet test failed (HTTP {res.status_code})'
    except Exception as exc:
        return False, f'NZBGet connection error: {exc}'


def _test_ssh(data: dict) -> tuple[bool, str]:
    ssh_host = data.get('ssh_host') or config_store.get_config('SSH_HOST')
    ssh_port = int(data.get('ssh_port') or config_store.get_config('SSH_PORT') or 22)
    ssh_user = data.get('ssh_user') or config_store.get_config('SSH_USER')
    ssh_pass = data.get('ssh_password') or ''
    if not ssh_pass or ssh_pass == '••••••••':
        ssh_pass = config_store.get_config('SSH_PASSWORD') or ''
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
            timeout=5
        )
        client.close()
        return True, f'SSH connection to {ssh_host}:{ssh_port} successful!'
    except Exception as exc:
        return False, f'SSH connection failed: {exc}'


# Registry mapping service names to test functions
CONNECTION_TESTERS: dict[str, callable] = {
    'sonarr': _test_sonarr,
    'radarr': _test_radarr,
    'plex': _test_plex,
    'nzbget': _test_nzbget,
    'ssh': _test_ssh,
}


def test_connection(service: str, data: dict) -> tuple[bool, str]:
    """Test a service connection. Returns (success, message)."""
    tester = CONNECTION_TESTERS.get(service.lower())
    if not tester:
        return False, 'Unknown service'
    return tester(data)


# ── Generic connection status checker ─────────────────────────────────────────

def _check_service_status(url: str, headers: dict, endpoint: str, timeout: int = 2) -> dict:
    """Generic connection status check for any HTTP service."""
    if not url:
        return {'status': 'Disconnected', 'ok': False}
    try:
        res = requests.get(f"{url}{endpoint}", headers=headers, timeout=timeout)
        status = "Connected" if res.status_code == 200 else f"Error ({res.status_code})"
    except Exception:
        status = "Unreachable"
    return {'status': status, 'ok': status == 'Connected'}


def check_sonarr_status() -> dict:
    url, key = get_sonarr_url(), get_sonarr_api_key()
    if not url or not key:
        return {'status': 'Disconnected', 'ok': False}
    return _check_service_status(url, {'X-Api-Key': key}, '/api/v3/system/status')


def check_radarr_status() -> dict:
    url, key = get_radarr_url(), get_radarr_api_key()
    if not url or not key:
        return {'status': 'Disconnected', 'ok': False}
    return _check_service_status(url, {'X-Api-Key': key}, '/api/v3/system/status')


def check_plex_status() -> dict:
    plex_url, plex_token = get_plex_credentials()
    if not plex_url or not plex_token:
        return {'status': 'Disconnected', 'ok': False}
    return _check_service_status(plex_url, {'X-Plex-Token': plex_token, 'Accept': 'application/json'}, '/identity')
