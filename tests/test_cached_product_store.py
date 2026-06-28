"""Tests for the per-worker product cache invalidation.

Regression: SQLite WAL mode writes go to the .db-wal sidecar, not the main
.db file. The cache used to only stat() the main file, so writes from other
workers were invisible until the next checkpoint. This test ensures the
cache notices touches to the WAL sidecar.
"""
import os
import time

from sale_monitor.web.app import _CachedProductStore


class _FakeStore:
    """Minimal stand-in for ProductStore — counts how many times get_all() is called."""
    def __init__(self, db_path):
        self.db_path = db_path
        self.calls = 0

    def get_all(self):
        self.calls += 1
        return [f"snapshot-{self.calls}"]


def _touch(path: str, mtime: float) -> None:
    """Create file if absent and set its mtime."""
    if not os.path.exists(path):
        with open(path, "wb"):
            pass
    os.utime(path, (mtime, mtime))


def test_cache_invalidates_when_wal_changes(tmp_path):
    """Writing to .db-wal alone (no .db change) must still invalidate the cache."""
    db = tmp_path / "history.db"
    wal = tmp_path / "history.db-wal"
    _touch(str(db), mtime=1000.0)
    _touch(str(wal), mtime=1000.0)

    store = _FakeStore(str(db))
    cache = _CachedProductStore(store)
    cache._TTL = 0  # bypass throttle for the test

    first = cache.get_all()
    assert store.calls == 1

    # Touch ONLY the WAL — simulates another worker writing under WAL mode
    # without checkpointing to the main .db file.
    _touch(str(wal), mtime=2000.0)

    second = cache.get_all()
    assert store.calls == 2  # cache refreshed despite .db mtime unchanged
    assert second != first


def test_cache_invalidates_when_shm_changes(tmp_path):
    """Sanity: .db-shm changes also trigger a refresh (covers checkpointing edge cases)."""
    db = tmp_path / "history.db"
    shm = tmp_path / "history.db-shm"
    _touch(str(db), mtime=1000.0)
    _touch(str(shm), mtime=1000.0)

    store = _FakeStore(str(db))
    cache = _CachedProductStore(store)
    cache._TTL = 0

    cache.get_all()
    _touch(str(shm), mtime=2000.0)
    cache.get_all()
    assert store.calls == 2


def test_cache_skips_refresh_when_nothing_changed(tmp_path):
    """No file touched → no requery within or across TTL windows."""
    db = tmp_path / "history.db"
    _touch(str(db), mtime=1000.0)

    store = _FakeStore(str(db))
    cache = _CachedProductStore(store)
    cache._TTL = 0

    cache.get_all()
    cache.get_all()
    cache.get_all()
    assert store.calls == 1


def test_cache_throttles_via_ttl(tmp_path):
    """Repeated calls within TTL window do not stat() — return cached snapshot."""
    db = tmp_path / "history.db"
    _touch(str(db), mtime=1000.0)

    store = _FakeStore(str(db))
    cache = _CachedProductStore(store)
    # Default TTL is 2s; reduce slightly so test runs fast but still exercises throttle
    cache._TTL = 5.0

    cache.get_all()
    _touch(str(db), mtime=2000.0)  # change happens inside the throttle window
    cache.get_all()  # should NOT re-stat or re-query
    assert store.calls == 1


def test_invalidate_forces_refresh(tmp_path):
    """Write-through invalidate() must force a refresh even within TTL."""
    db = tmp_path / "history.db"
    _touch(str(db), mtime=1000.0)

    store = _FakeStore(str(db))
    cache = _CachedProductStore(store)
    cache._TTL = 60.0  # very long throttle to prove invalidate is what unblocks

    cache.get_all()
    assert store.calls == 1
    cache.invalidate()
    cache.get_all()
    assert store.calls == 2
