"""Small shared helpers used across the CLI and web layers."""
from datetime import datetime, timezone
import os

_TRUTHY = ("1", "true", "yes", "y", "on")


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable consistently."""
    val = os.getenv(name)
    if val is None or not str(val).strip():
        return default
    return str(val).strip().lower() in _TRUTHY


def str_to_bool(v, default: bool = False) -> bool:
    """Parse a truthy string ('1', 'true', 'yes', 'y', 'on')."""
    if v is None:
        return default
    return str(v).strip().lower() in _TRUTHY


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with offset.

    All persisted timestamps (history rows, state entries, cutoff
    comparisons) must use this so lexicographic comparison stays valid.
    """
    return datetime.now(timezone.utc).isoformat()


def parse_iso(ts: str):
    """Parse an ISO timestamp defensively; naive values are assumed UTC.

    Returns an aware datetime, or None if unparseable.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
