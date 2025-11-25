"""Simple JSON config store for global application settings.

Currently supports:
  - base_currency: target currency code for normalized display (default 'CAD')

Config file structure example:
{
  "base_currency": "CAD"
}
"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, Any

DEFAULTS: Dict[str, Any] = {
    "base_currency": "CAD",
}


def load_config(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return DEFAULTS.copy()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULTS.copy()
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
    with NamedTemporaryFile("w", delete=False, dir=str(p.parent), encoding="utf-8") as tmp:
        json.dump(merged, tmp, indent=2, sort_keys=True)
        tmp.flush()
    Path(tmp.name).replace(p)


def get_base_currency(path: str) -> str:
    cfg = load_config(path)
    cur = str(cfg.get("base_currency", "CAD")).upper()
    return cur or "CAD"
