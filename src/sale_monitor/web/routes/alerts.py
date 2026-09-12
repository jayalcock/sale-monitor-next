"""Alert and failure-diagnostics endpoints."""
import os
import sqlite3

from flask import Blueprint, current_app, jsonify, request

from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.web.auth import require_api_key_for_reads
from sale_monitor.web.comparison import build_comparison_groups
from sale_monitor.web.helpers import (
    config_file,
    get_history,
    get_product_store,
    get_state_cache,
    paginate,
    safe_error,
)

bp = Blueprint('alerts', __name__)


@bp.route('/api/alerts')
@require_api_key_for_reads
def api_alerts():
    """Get products that have hit their price targets or discount thresholds."""
    try:
        # Cached result is valid while the state file is unchanged; product
        # mutations (add/update/toggle/delete) invalidate it explicitly.
        cache = current_app.config['_ALERTS_CACHE']
        state_path = current_app.config['STATE_FILE']
        try:
            st_mtime = os.path.getmtime(state_path)
        except OSError:
            st_mtime = 0.0
        if cache['mtime'] is not None and st_mtime == cache['mtime']:
            return jsonify(cache['data'])

        products = get_product_store().get_all()
        state = get_state_cache().get()
        history = get_history()

        alerts = []
        failure_threshold = float(os.getenv('ALERT_FAILURE_THRESHOLD', '50'))  # 50% failure rate
        min_checks = int(os.getenv('ALERT_MIN_CHECKS', '3'))  # Minimum 3 checks to report

        # Pre-fetch failure stats and max prices in batch queries
        enabled_products = [p for p in products if p.enabled]
        enabled_urls = [p.url for p in enabled_products]
        batch_failure = history.get_failure_stats_batch(enabled_urls, days=7)
        # Check if the last 3 checks succeeded (suppresses alert for fixed products)
        batch_recent_status = history.get_recent_success_batch(enabled_urls, n=3)
        discount_urls = [p.url for p in enabled_products if p.discount_threshold]
        batch_max = history.get_max_prices_batch(discount_urls, days=30) if discount_urls else {}

        for p in enabled_products:
            state_data = state.get(p.url, {})
            current = state_data.get('current_price')

            # Check for price alerts
            if current is not None:
                alert_type = None
                message = None

                currency = (state_data.get('currency') or '').upper()
                cur_prefix = f"[{currency}] " if currency else ""

                # Check target price
                if p.target_price and current <= p.target_price:
                    alert_type = 'target_met'
                    message = f'{cur_prefix}Price ${current:.2f} is at or below target ${p.target_price:.2f}'

                # Check discount threshold - use batch max prices
                elif p.discount_threshold:
                    max_price = batch_max.get(p.url)

                    if max_price and max_price > current:
                        discount = ((max_price - current) / max_price) * 100
                        if discount >= p.discount_threshold:
                            alert_type = 'discount_met'
                            message = f'{cur_prefix}Price dropped {discount:.1f}% (${max_price:.2f} → ${current:.2f})'

                if alert_type:
                    alerts.append({
                        'name': p.name,
                        'url': p.url,
                        'current_price': current,
                        'alert_type': alert_type,
                        'message': message,
                        'last_checked': state_data.get('last_checked')
                    })

            # Check for high failure rate (last 7 days) — from batch result
            # Suppress if the most recent checks are all successes (problem is fixed)
            failure_stats = batch_failure.get(p.url)
            recent_ok = batch_recent_status.get(p.url, False)
            if not recent_ok and failure_stats and (failure_stats['total_checks'] >= min_checks and
                failure_stats['failure_rate'] >= failure_threshold):
                alerts.append({
                    'name': p.name,
                    'url': p.url,
                    'current_price': current,
                    'alert_type': 'high_failure',
                    'message': f"Price extraction failing {failure_stats['failure_rate']:.0f}% of the time ({failure_stats['failed_checks']}/{failure_stats['total_checks']} checks)",
                    'last_checked': state_data.get('last_checked'),
                    'failure_stats': failure_stats
                })

        # Optional competitive alerts (gated by env to avoid test disruption)
        enable_competitive = os.getenv('ENABLE_COMPETITIVE_ALERTS', '0').strip().lower() in ('1', 'true', 'yes')
        if enable_competitive:
            try:
                base_currency = get_base_currency(config_file()).upper()
                groups = build_comparison_groups(state, products, base_currency)
                threshold_pct = float(os.getenv('ALERT_COMPETITIVE_DROP_PERCENT', '5'))
                for g in groups:
                    # Use items with valid base price
                    priced = [it for it in g['items'] if isinstance(it.get('price_in_base'), (int, float))]
                    if len(priced) < 2:
                        continue
                    # Sorted ascending from helper; first is lowest
                    lowest = priced[0]
                    second = priced[1]
                    try:
                        if second['price_in_base'] > 0:
                            diff_pct = ((second['price_in_base'] - lowest['price_in_base']) / second['price_in_base']) * 100
                        else:
                            diff_pct = 0.0
                    except (TypeError, ValueError, KeyError):
                        diff_pct = 0.0
                    if diff_pct >= threshold_pct:
                        alerts.append({
                            'name': lowest['name'],
                            'url': lowest['url'],
                            'current_price': lowest['current_price'],
                            'alert_type': 'competitive_lowest',
                            'message': f"Lowest among competitors (−{diff_pct:.1f}% vs next). Group: {g['group_key']}",
                            'last_checked': lowest.get('last_checked'),
                            'group_key': g['group_key']
                        })
            except (OSError, ValueError, KeyError):
                pass

        current_app.config['_ALERTS_CACHE'] = {'mtime': st_mtime, 'data': alerts}
        return jsonify(alerts)
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/failures')
@require_api_key_for_reads
def api_failures():
    """Get detailed failure information for all products."""
    try:
        products = get_product_store().get_all()
        state = get_state_cache().get()
        history = get_history()

        days = int(request.args.get('days', 7))

        # Single batch query instead of per-product loop
        all_urls = [p.url for p in products]
        batch_stats = history.get_failure_stats_batch(all_urls, days=days)

        failures = []
        for p in products:
            failure_stats = batch_stats.get(p.url)
            if not failure_stats or failure_stats['failed_checks'] <= 0:
                continue
            state_data = state.get(p.url, {})
            failures.append({
                'name': p.name,
                'url': p.url,
                'enabled': p.enabled,
                'total_checks': failure_stats['total_checks'],
                'failed_checks': failure_stats['failed_checks'],
                'failure_rate': failure_stats['failure_rate'],
                'last_success': failure_stats.get('last_success'),
                'last_failure': failure_stats.get('last_failure'),
                'last_checked': state_data.get('last_checked'),
                'current_price': state_data.get('current_price'),
                'currency': state_data.get('currency', 'CAD')
            })

        # Sort by failure rate descending, then by failed count
        failures.sort(key=lambda x: (x['failure_rate'], x['failed_checks']), reverse=True)

        return jsonify(paginate(failures))
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)
