"""Health-check endpoints."""
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify

from sale_monitor.utils import parse_iso
from sale_monitor.web.auth import require_api_key_for_reads
from sale_monitor.web.helpers import get_product_store, get_state_cache

logger = logging.getLogger(__name__)

bp = Blueprint('health', __name__)


@bp.route('/api/health')
@require_api_key_for_reads
def api_health():
    """Lightweight health check for Docker/uptime probes."""
    try:
        with sqlite3.connect(current_app.config['HISTORY_DB']) as conn:
            conn.execute("SELECT 1 FROM price_history LIMIT 1")
        return jsonify({'status': 'ok'})
    except Exception:  # noqa: BLE001 - health probe must never raise
        return jsonify({'status': 'error'}), 500


@bp.route('/api/health/detailed')
@require_api_key_for_reads
def api_health_detailed():
    """Detailed health check: DB integrity, product counts, uptime, cache age."""
    try:
        # DB integrity + row count
        db_ok = False
        rows = 0
        try:
            with sqlite3.connect(current_app.config['HISTORY_DB']) as conn:
                ok_row = conn.execute("PRAGMA integrity_check").fetchone()
                db_ok = bool(ok_row and ok_row[0] == 'ok')
                row = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()
                rows = int(row[0]) if row else 0
        except sqlite3.Error:
            pass

        # DB file size
        db_size_mb = None
        try:
            db_size_mb = round(os.path.getsize(current_app.config['HISTORY_DB']) / (1024 * 1024), 2)
        except OSError:
            pass

        # Product counts
        product_count = 0
        enabled_count = 0
        try:
            products = get_product_store().get_all()
            product_count = len(products)
            enabled_count = sum(1 for p in products if p.enabled)
        except (OSError, ValueError):
            pass

        # Last check time from state
        last_check = None
        try:
            state = get_state_cache().get()
            timestamps = [
                rec.get('last_checked', '')
                for rec in state.values()
                if isinstance(rec, dict) and rec.get('last_checked')
            ]
            if timestamps:
                last_check = max(timestamps)
        except (OSError, ValueError):
            pass

        # Exchange rate cache age
        exchange_rate_age = None
        try:
            with sqlite3.connect(current_app.config['HISTORY_DB']) as conn:
                er_row = conn.execute(
                    "SELECT MAX(timestamp) FROM exchange_rates"
                ).fetchone()
                if er_row and er_row[0]:
                    cached_at = parse_iso(er_row[0])
                    if cached_at is not None:
                        exchange_rate_age = round(
                            (datetime.now(timezone.utc) - cached_at).total_seconds()
                        )
        except (sqlite3.Error, ValueError, TypeError):
            pass

        uptime_seconds = round(time.time() - current_app.config['_APP_START_TIME'])

        return jsonify({
            'status': 'ok' if db_ok else 'degraded',
            'integrity_ok': db_ok,
            'rows': rows,
            'db_size_mb': db_size_mb,
            'product_count': product_count,
            'enabled_count': enabled_count,
            'last_check': last_check,
            'exchange_rate_cache_age_seconds': exchange_rate_age,
            'uptime_seconds': uptime_seconds,
        })
    except (sqlite3.Error, OSError, ValueError) as e:
        logger.error("Health check failed: %s", e, exc_info=True)
        return jsonify({'status': 'error', 'error': 'Health check failed'}), 500
