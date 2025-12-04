import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict

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