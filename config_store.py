"""
config_store.py
----------------
Manages encrypted storage and retrieval of application configuration settings.
Uses AES-128 Fernet symmetric encryption to persist settings in /config/settings.enc.
Initializes from environment variables on first launch if no config file exists.
"""
from __future__ import annotations

import os
import json
import logging
from typing import Any
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

KEY_FILE = os.path.join(CONFIG_DIR, 'secret.key')
SETTINGS_FILE = os.path.join(CONFIG_DIR, 'settings.enc')

# ── Key Management ───────────────────────────────────────────────────────────

def _get_or_create_key() -> bytes:
    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, 'rb') as f:
                key = f.read().strip()
                if key:
                    return key
        except Exception as exc:
            logger.warning(f"[ConfigStore] Failed reading key file: {exc}")

    key = Fernet.generate_key()
    try:
        with open(KEY_FILE, 'wb') as f:
            f.write(key)
        os.chmod(KEY_FILE, 0o600)
        logger.info("[ConfigStore] Created new secret encryption key.")
    except Exception as exc:
        logger.warning(f"[ConfigStore] Could not write secret key file: {exc}")
    return key


_cipher = Fernet(_get_or_create_key())

# ── Default Configuration ─────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    'SONARR_URL': '',
    'SONARR_API_KEY': '',
    'RADARR_URL': '',
    'RADARR_API_KEY': '',
    'NZBGET_URL': '',
    'NZBGET_USERNAME': '',
    'NZBGET_PASSWORD': '',
    'PLEX_URL': '',
    'PLEX_TOKEN': '',
    'PLEX_WATCH_INTERVAL': 3600,
    'PLEX_POLL_INTERVAL': 1200,
    'PLEX_IDLE_POLLS': 3,
    'PLEX_SHUTDOWN_DRY_RUN': True,
    'ROLLING_WINDOW': 3,
    'SSH_HOST': '172.17.0.1',
    'SSH_PORT': 22,
    'SSH_USER': '',
    'SSH_KEY_PATH': '/root/.ssh/id_rsa',
}

_current_config: dict[str, Any] = {}

# ── Storage Operations ────────────────────────────────────────────────────────

def _load_initial_config() -> dict[str, Any]:
    """Load settings from encrypted file, or fallback to environment vars."""
    config = dict(DEFAULT_CONFIG)

    # 1. Try reading encrypted file
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'rb') as f:
                encrypted_data = f.read()
            decrypted_json = _cipher.decrypt(encrypted_data).decode('utf-8')
            saved_dict = json.loads(decrypted_json)
            config.update(saved_dict)
            logger.info("[ConfigStore] Loaded encrypted configuration settings.")
            return config
        except Exception as exc:
            logger.error(f"[ConfigStore] Error reading encrypted settings file: {exc}")

    # 2. Fallback / Migrate from environment variables on first run
    env_migrated = False
    for k in DEFAULT_CONFIG:
        env_val = os.environ.get(k)
        if env_val is not None and env_val != '':
            env_migrated = True
            # Type casting for integers and booleans
            if isinstance(DEFAULT_CONFIG[k], int):
                try:
                    config[k] = int(env_val)
                except ValueError:
                    config[k] = DEFAULT_CONFIG[k]
            elif isinstance(DEFAULT_CONFIG[k], bool):
                config[k] = env_val.lower() in ('true', '1', 'yes')
            else:
                config[k] = env_val

    # Save initial settings to encrypted storage
    save_config(config)
    if env_migrated:
        logger.info("[ConfigStore] Migrated environment variables to encrypted settings file.")
    return config


def save_config(new_settings: dict[str, Any]) -> dict[str, Any]:
    """Encrypt and save configuration settings to disk."""
    global _current_config

    # Merge into current config
    updated = dict(_current_config)
    for k, v in new_settings.items():
        if k in DEFAULT_CONFIG:
            target_type = type(DEFAULT_CONFIG[k])
            if target_type == int:
                try:
                    updated[k] = int(v)
                except (ValueError, TypeError):
                    updated[k] = DEFAULT_CONFIG[k]
            elif target_type == bool:
                if isinstance(v, bool):
                    updated[k] = v
                else:
                    updated[k] = str(v).lower() in ('true', '1', 'yes')
            else:
                updated[k] = str(v).strip()

    _current_config = updated

    try:
        json_str = json.dumps(_current_config)
        encrypted = _cipher.encrypt(json_str.encode('utf-8'))
        with open(SETTINGS_FILE, 'wb') as f:
            f.write(encrypted)
        os.chmod(SETTINGS_FILE, 0o600)
        logger.info("[ConfigStore] Successfully saved encrypted configuration to disk.")
    except Exception as exc:
        logger.error(f"[ConfigStore] Failed saving encrypted settings: {exc}")

    return _current_config


def get_all_config() -> dict[str, Any]:
    """Return a dictionary of all configuration settings."""
    global _current_config
    if not _current_config:
        _current_config = _load_initial_config()
    return dict(_current_config)


def get_config(key: str, default: Any = None) -> Any:
    """Get a single configuration value by key."""
    cfg = get_all_config()
    return cfg.get(key, default if default is not None else DEFAULT_CONFIG.get(key))


# Initialize on module load
_current_config = _load_initial_config()
