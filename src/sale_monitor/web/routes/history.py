"""Price history and stats endpoints."""
import csv
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from io import StringIO

from flask import Blueprint, Response, current_app, jsonify, request

from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.web.auth import require_api_key_for_reads
from sale_monitor.web.comparison import build_comparison_groups
from sale_monitor.web.helpers import (
    config_file,
    get_ex_service,
    get_history,
    get_product_store,
    get_state_cache,
    paginate,
    safe_error,
)

bp = Blueprint('history', __name__)


@bp.route('/api/product/stats')
@require_api_key_for_reads
def api_product_stats():
    """Get statistics for a product."""
    try:
        url = request.args.get('url')
        group_key = request.args.get('group_key')
        if not url and not group_key:
            return jsonify({'error': 'URL or group_key parameter required'}), 400

        history = get_history()
        days = int(request.args.get('days', 30))

        # Group stats: aggregate across all URLs in the group
        if group_key:
            # Build groups and find members
            products = get_product_store().get_all()
            state = get_state_cache().get()
            base_currency = get_base_currency(config_file()).upper()
            groups = build_comparison_groups(state or {}, products or [], base_currency)
            group = next((g for g in groups if g.get('group_key') == group_key), None)
            if not group:
                return jsonify({'error': 'Group not found'}), 404
            # Combine histories newest-first from DB
            urls = [it['url'] for it in group.get('items', []) if it.get('url')]
            records = []
            for u in urls:
                recs = history.get_history_extended(u, days=days)
                for (ts, price, status, currency, _) in recs:
                    if status == 'success':
                        records.append((ts, price, currency))
            if not records:
                return jsonify({'error': 'No data'}), 404
            # Compute stats on base prices
            ex_service = get_ex_service()
            base_prices = []
            for (ts, price, currency) in records:
                try:
                    if currency.upper() == base_currency:
                        base_prices.append(float(price))
                    else:
                        converted = ex_service.convert(float(price), currency.upper(), base_currency)
                        if converted is not None:
                            base_prices.append(float(converted))
                except (ValueError, TypeError, AttributeError):
                    pass
            if not base_prices:
                return jsonify({'error': 'No data'}), 404
            return jsonify({
                'current_price': base_prices[-1],
                'min_price': min(base_prices),
                'max_price': max(base_prices),
                'avg_price': sum(base_prices) / len(base_prices),
                'checks_count': len(base_prices),
                'first_check': None,
                'last_check': None,
            })
        else:
            stats = history.get_stats(url, days=days)
            return jsonify(stats)
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/product/history')
@require_api_key_for_reads
def api_product_history():
    """Get price history for a single product.

    Response format: list of objects [{timestamp, price, currency, price_in_base}] newest-first.
    price_in_base is always expressed in current configured base currency.
    """
    try:
        url = request.args.get('url')
        group_key = request.args.get('group_key')
        if not url and not group_key:
            return jsonify({'error': 'URL or group_key parameter required'}), 400

        days = int(request.args.get('days', 30))
        history = get_history()
        base_currency = get_base_currency(config_file()).upper()
        records = []
        if group_key:
            # Build groups and aggregate histories from members
            products = get_product_store().get_all()
            state = get_state_cache().get()
            groups = build_comparison_groups(state or {}, products or [], base_currency)
            group = next((g for g in groups if g.get('group_key') == group_key), None)
            if not group:
                return jsonify([])
            urls = [it['url'] for it in group.get('items', []) if it.get('url')]
            for u in urls:
                records.extend(history.get_history_extended(u, days=days))
        else:
            records = history.get_history_extended(url, days=days)

        # If no DB records, synthesize one from state so chart isn't blank
        if not records and url:
            st = get_state_cache().get().get(url)
            if st and 'current_price' in st:
                cur = st.get('current_price')
                cur_currency = (st.get('currency') or 'CAD').upper()
                records = [
                    (st.get('last_checked') or datetime.now(timezone.utc).isoformat(), cur, 'success', cur_currency, None)
                ]

        result = []
        for (ts, price, status, currency, stored_price_cad) in records:
            if status != 'success':
                continue

            # Use stored base-currency price (recorded at check time with
            # the exchange rate that was current then).  No live fallback
            # — using today's rate would hide real exchange-rate variation.
            price_in_base = None
            try:
                if stored_price_cad is not None:
                    price_in_base = float(stored_price_cad)
                elif price is not None and currency and currency.upper() == base_currency:
                    price_in_base = price
            except (ValueError, TypeError):
                pass

            # Round to 2 decimals when available
            try:
                if price_in_base is not None:
                    price_in_base = round(float(price_in_base), 2)
            except (TypeError, ValueError):
                pass

            result.append({
                'timestamp': ts,
                'price': price,
                'currency': currency,
                'price_in_base': price_in_base,
                'base_currency': base_currency
            })

        # If grouping: collapse by date (lowest base price per day)
        if group_key and result:
            day_map = defaultdict(list)
            for r in result:
                d = r['timestamp'][:10]
                if r.get('price_in_base') is not None:
                    day_map[d].append(r)
            collapsed = []
            for d, rs in day_map.items():
                # pick lowest base price entry
                rs.sort(key=lambda x: x.get('price_in_base') if x.get('price_in_base') is not None else float('inf'))
                collapsed.append(rs[0])
            # Sort by timestamp ascending
            collapsed.sort(key=lambda x: x['timestamp'])
            return jsonify(collapsed)
        else:
            return jsonify(result)
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/history/all')
@require_api_key_for_reads
def api_history_all():
    """Get price history time series for all products.

    Query params:
    - days: int (optional, default 30) number of recent days to include
    Response format per product:
    {
      url, name,
      series: [ {timestamp, price, currency, price_in_base, base_currency}, ... ]
    }
    """
    try:
        days = int(request.args.get('days', 30))
        history = get_history()
        base_currency = get_base_currency(config_file()).upper()
        # Prefer names from products DB
        try:
            current_products = get_product_store().get_all()
            name_by_url = {p.url: p.name for p in current_products}
        except (OSError, ValueError):
            name_by_url = {}

        # Restrict to current product URLs when available
        url_filter = set(name_by_url.keys()) if name_by_url else None

        # Downsampled query: one record per product per day (latest
        # successful), done in SQL to avoid pulling 90K+ rows into Python.
        daily = history.get_daily_history(days=days, url_filter=url_filter)

        result = []
        urls_to_process = set(daily.keys())
        if name_by_url:
            urls_to_process |= set(name_by_url.keys())

        for url in urls_to_process:
            if name_by_url and url not in name_by_url:
                continue
            day_records = daily.get(url)
            if not day_records:
                # Fallback: synthesize a single point from current state
                try:
                    st = get_state_cache().get().get(url)
                    if st and 'current_price' in st:
                        cur = st.get('current_price')
                        cur_currency = (st.get('currency') or 'CAD').upper()
                        day_records = [
                            (st.get('last_checked') or datetime.now(timezone.utc).isoformat(), cur, cur_currency, None)
                        ]
                    else:
                        continue
                except (ValueError, TypeError, sqlite3.Error):
                    continue
            display_name = name_by_url.get(url, url)

            series = []
            for (ts, price, currency, stored_cad) in day_records:
                price_in_base = None
                try:
                    if stored_cad is not None:
                        price_in_base = round(float(stored_cad), 2)
                    elif currency and currency.upper() == base_currency:
                        price_in_base = price
                except (ValueError, TypeError):
                    pass

                series.append({
                    'timestamp': ts,
                    'price': price,
                    'currency': currency,
                    'price_in_base': price_in_base,
                    'base_currency': base_currency
                })
            if not series:
                continue
            result.append({
                'url': url,
                'name': display_name,
                'series': series
            })

        return jsonify(paginate(result))
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/export/history')
@require_api_key_for_reads
def api_export_history():
    """Export all price history as CSV."""
    try:
        # Legacy export format to maintain backward compatibility with tests
        with sqlite3.connect(current_app.config['HISTORY_DB']) as conn:
            cursor = conn.execute(
                """
                SELECT product_name, product_url, price, timestamp, check_status
                FROM price_history
                ORDER BY timestamp DESC
                """
            )
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(['product_name', 'product_url', 'price', 'timestamp', 'status'])
            for row in cursor:
                # row = (product_name, product_url, price, timestamp, check_status)
                writer.writerow(row)
            output.seek(0)

        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename=price_history_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
            }
        )
    except (OSError, sqlite3.Error) as e:
        return safe_error(e)
