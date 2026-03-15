import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Set

from sale_monitor.storage.file_lock import FileLock


def load_state(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    
    lock = FileLock(path)
    try:
        lock.acquire()
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    finally:
        lock.release()


def save_state(path: str, data: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    
    lock = FileLock(path)
    try:
        lock.acquire()
        # Atomic write
        with NamedTemporaryFile("w", delete=False, dir=str(p.parent), encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.flush()
        Path(tmp.name).replace(p)
    finally:
        lock.release()


def prune_stale_entries(
    state_path: str,
    active_urls: Set[str],
    max_identifiers: int = 50,
) -> int:
    """Remove state entries for URLs not in *active_urls* and cap identifiers.

    Returns the number of stale entries removed.
    """
    state = load_state(state_path)
    stale = [url for url in state if url not in active_urls]
    for url in stale:
        del state[url]

    # Cap identifiers dict size per product
    for rec in state.values():
        ids = rec.get("identifiers")
        if isinstance(ids, dict) and len(ids) > max_identifiers:
            # Keep only the last N items (dict preserves insertion order in 3.7+)
            keys = list(ids.keys())
            for k in keys[:-max_identifiers]:
                del ids[k]

    save_state(state_path, state)
    return len(stale)