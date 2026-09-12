"""Shared helpers for the web blueprints: caches, pagination, errors."""
import logging
import os
import time

from flask import current_app, jsonify, request

from sale_monitor.storage.json_state import load_state

logger = logging.getLogger(__name__)


class CachedState:
    """Read state.json only when the file has changed (mtime-based)."""

    def __init__(self, path: str):
        self._path = path
        self._mtime: float = 0.0
        self._data: dict = {}

    def get(self) -> dict:
        try:
            mt = os.path.getmtime(self._path)
        except OSError:
            return {}
        if mt != self._mtime:
            self._data = load_state(self._path)
            self._mtime = mt
        return self._data


class CachedProductStore:
    """Thin cache over ProductStore.get_all() that re-queries only when the DB file changes."""

    _TTL = 2.0  # seconds – avoids stat() storms while still feeling fresh

    def __init__(self, store):
        self._store = store
        self._db_path = store.db_path
        self._mtime: float = 0.0
        # -inf, not 0: time.monotonic() counts from boot, so on a recently
        # booted machine (CI VMs!) `now - 0 < TTL` would wrongly skip the
        # very first refresh.
        self._last_check: float = float('-inf')
        self._products: list = []

    def _latest_mtime(self):
        """Newest mtime across the DB file and its WAL/SHM sidecars.

        Under WAL mode, writes land in the -wal sidecar without touching the
        main .db file, so watching the .db mtime alone misses writes made by
        other processes until the next checkpoint.
        """
        latest = None
        for path in (self._db_path, self._db_path + '-wal', self._db_path + '-shm'):
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            latest = mt if latest is None else max(latest, mt)
        return latest

    def get_all(self):
        now = time.monotonic()
        if now - self._last_check < self._TTL:
            return self._products
        self._last_check = now
        mt = self._latest_mtime()
        if mt is None:
            return self._products
        if mt != self._mtime:
            self._products = self._store.get_all()
            self._mtime = mt
        return self._products

    def invalidate(self):
        """Force a refresh on next access (call after writes)."""
        self._mtime = 0.0
        self._last_check = float('-inf')

    # Write-through methods that invalidate the cache
    def add(self, *args, **kwargs):
        result = self._store.add(*args, **kwargs)
        self.invalidate()
        return result

    def update(self, *args, **kwargs):
        result = self._store.update(*args, **kwargs)
        self.invalidate()
        return result

    def delete(self, *args, **kwargs):
        result = self._store.delete(*args, **kwargs)
        self.invalidate()
        return result

    def replace_all(self, *args, **kwargs):
        result = self._store.replace_all(*args, **kwargs)
        self.invalidate()
        return result

    def import_from_csv(self, *args, **kwargs):
        result = self._store.import_from_csv(*args, **kwargs)
        self.invalidate()
        return result

    def __getattr__(self, name):
        """Delegate everything else to the underlying store."""
        return getattr(self._store, name)


# ── per-app object accessors ───────────────────────────────────────────────

def get_product_store():
    return current_app.config['PRODUCT_STORE']


def get_history():
    return current_app.config['_HISTORY']


def get_ex_service():
    return current_app.config['_EX_SERVICE']

def get_state_cache() -> CachedState:
    return current_app.config['_STATE_CACHE']


def get_check_service():
    return current_app.config['_CHECK_SERVICE']


def get_image_service():
    return current_app.config['_IMAGE_SERVICE']


def config_file() -> str:
    return current_app.config['CONFIG_FILE']


def state_file() -> str:
    return current_app.config['STATE_FILE']


def invalidate_alerts_cache():
    """Drop the cached /api/alerts payload (call after product mutations)."""
    current_app.config['_ALERTS_CACHE'] = {'mtime': None, 'data': []}


# ── request helpers ────────────────────────────────────────────────────────

def safe_error(e, msg='Internal server error'):
    """Log full exception server-side but return a generic message to clients."""
    logger.error("%s: %s", msg, e, exc_info=True)
    return jsonify({'error': msg}), 500


def paginate(items: list) -> dict:
    """Apply limit/offset pagination from query params. Returns envelope dict."""
    limit = request.args.get('limit', type=int)
    offset = request.args.get('offset', 0, type=int)
    total = len(items)
    if limit is not None:
        limit = max(1, min(limit, 200))
        items = items[offset:offset + limit]
    else:
        limit = total
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset}
