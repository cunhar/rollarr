"""
services/activity_log.py
-------------------------
Manages the in-memory activity log (last 20 entries) with thread-safe access
and persistent storage to /config/history.json.

Extracted from app.py to keep routing logic separate from log storage.
"""
from __future__ import annotations

import os
import json
import threading
import logging
from collections import deque

from integrations.common import now_str

logger = logging.getLogger(__name__)

# Persistent configuration directory
CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HISTORY_FILE = os.path.join(CONFIG_DIR, 'history.json')

# In-memory activity log (last 20 entries) with thread lock
_history: deque = deque(maxlen=20)
_lock = threading.Lock()


def load():
    """Load persisted activity log entries from disk."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                data = json.load(f)
                with _lock:
                    _history.clear()
                    for item in data:
                        _history.append(item)
            logger.info(f"Loaded {len(data)} activity log entries from {HISTORY_FILE}")
        except Exception as e:
            logger.warning(f"Could not load activity log file: {e}")


def save():
    """Persist current activity log to disk."""
    try:
        with _lock:
            data = list(_history)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save activity log file: {e}")


def log_call(status: str, message: str, payload: dict = None):
    """Append a new entry to the activity log and persist to disk."""
    entry = {
        "timestamp": now_str(),
        "status": status,
        "message": message,
        "payload": payload,
    }
    with _lock:
        _history.appendleft(entry)
    save()


def get_entries() -> list[dict]:
    """Return a snapshot of all activity log entries (thread-safe)."""
    with _lock:
        return list(_history)


def clear():
    """Clear all activity log entries and persist the empty state."""
    with _lock:
        _history.clear()
    save()
