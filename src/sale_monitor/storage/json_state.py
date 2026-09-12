import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Set

from sale_monitor.storage.file_lock import FileLock


def _read_unlocked(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_unlocked(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write
    with NamedTemporaryFile("w", delete=False, dir=str(p.parent), encoding="utf-8") as tmp:
        json.dump(data, tmp, indent=2, sort_keys=True)
        tmp.flush()
    Path(tmp.name).replace(p)


def load_state(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with FileLock(path):
        return _read_unlocked(p)


def save_state(path: str, data: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)  # lock file lives in parent dir
    with FileLock(path):
        _write_unlocked(p, data)


def mutate_state(path: str, mutator) -> Dict[str, Any]:
    """Read-modify-write the state file atomically under the lock.

    *mutator* is called with the freshly-read state dict and mutates it in
    place.  Unlike load-modify-save at the caller, concurrent writers (web
    endpoints vs. the CLI check cycle) can never clobber unrelated entries,
    because the merge happens against the current file contents.

    Returns the resulting full state dict.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)  # lock file lives in parent dir
    with FileLock(path):
        state = _read_unlocked(p)
        mutator(state)
        _write_unlocked(p, state)
        return state


def delete_state_entry(path: str, key: str) -> bool:
    """Remove one entry from the state file under the lock."""
    p = Path(path)
    with FileLock(path):
        state = _read_unlocked(p)
        if key not in state:
            return False
        del state[key]
        _write_unlocked(p, state)
        return True


def prune_stale_entries(
    state_path: str,
    active_urls: Set[str],
    max_identifiers: int = 50,
) -> int:
    """Remove state entries for URLs not in *active_urls* and cap identifiers.

    Returns the number of stale entries removed.
    """
    p = Path(state_path)
    with FileLock(state_path):
        state = _read_unlocked(p)
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

        _write_unlocked(p, state)
    return len(stale)
