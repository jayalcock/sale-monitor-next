"""Simple JSON config store for global application settings.

Currently supports:
  - base_currency: target currency code for normalized display (default 'CAD')
  - notifications: notification channel configuration (SMTP, webhooks, defaults)

Config file structure example:
{
  "base_currency": "CAD",
  "notifications": {
    "smtp": {
      "server": "", "port": 587, "username": "", "password": "",
      "from_email": "", "to_email": "", "enabled": false, "use_starttls": true
    },
    "webhooks": [],
    "default_channels": ["smtp"]
  }
}
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, Any

from sale_monitor.storage.file_lock import FileLock

DEFAULTS: Dict[str, Any] = {
    "base_currency": "CAD",
}

NOTIFICATION_DEFAULTS: Dict[str, Any] = {
    "smtp": {
        "server": "",
        "port": 587,
        "username": "",
        "password": "",
        "from_email": "",
        "to_email": "",
        "enabled": False,
        "use_starttls": True,
    },
    "webhooks": [],
    "default_channels": ["smtp"],
}


def load_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return DEFAULTS.copy()
    lock = FileLock(path)
    try:
        lock.acquire()
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULTS.copy()
    finally:
        lock.release()
    # Merge defaults for missing keys
    merged = DEFAULTS.copy()
    if isinstance(data, dict):
        merged.update({k: v for k, v in data.items() if v is not None})
    return merged


def save_config(path: str, data: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    merged = DEFAULTS.copy()
    if data:
        merged.update(data)
    lock = FileLock(path)
    try:
        lock.acquire()
        with NamedTemporaryFile("w", delete=False, dir=str(p.parent), encoding="utf-8") as tmp:
            json.dump(merged, tmp, indent=2, sort_keys=True)
            tmp.flush()
        Path(tmp.name).replace(p)
    finally:
        lock.release()


def get_base_currency(path: str) -> str:
    cfg = load_config(path)
    cur = str(cfg.get("base_currency", "CAD")).upper()
    return cur or "CAD"


def load_notification_config(path: str) -> Dict[str, Any]:
    """Load notification settings from config, merging with defaults."""
    cfg = load_config(path)
    stored = cfg.get("notifications")
    if not isinstance(stored, dict):
        return _deep_copy_dict(NOTIFICATION_DEFAULTS)
    merged = _deep_copy_dict(NOTIFICATION_DEFAULTS)
    # Merge SMTP settings
    if isinstance(stored.get("smtp"), dict):
        merged["smtp"].update(stored["smtp"])
    # Webhooks are stored as-is (list of dicts)
    if isinstance(stored.get("webhooks"), list):
        merged["webhooks"] = stored["webhooks"]
    # Default channels
    if isinstance(stored.get("default_channels"), list):
        merged["default_channels"] = stored["default_channels"]
    return merged


def save_notification_config(path: str, notif_data: Dict[str, Any]) -> None:
    """Save notification settings into the config file."""
    cfg = load_config(path)
    cfg["notifications"] = notif_data
    save_config(path, cfg)


def _deep_copy_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Simple deep copy for JSON-compatible dicts."""
    return json.loads(json.dumps(d))
