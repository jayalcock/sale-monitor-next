"""
Flask web application for Sale Monitor dashboard.
"""
from flask import Flask, render_template, jsonify, request, Response
from flask import send_file
from datetime import datetime, timezone
import os
import csv
import sqlite3
import time
import requests
import logging
from urllib.parse import urlparse

from sale_monitor.storage.csv_products import export_products_csv
from sale_monitor.storage.json_state import load_state, save_state
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.storage.product_store import ProductStore
from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.services.exchange_rates import ExchangeRateService
from sale_monitor.domain.models import Product
from sale_monitor.storage.file_lock import FileLock
from sale_monitor.storage.config_store import load_config, save_config, get_base_currency, load_notification_config, save_notification_config
from sale_monitor.web.auth import require_api_key, require_api_key_for_reads

logger = logging.getLogger(__name__)


def create_app():
    """Create and configure Flask application."""
    flask_app = Flask(__name__)
    
    # Configuration
    flask_app.config['PRODUCTS_CSV'] = os.getenv('PRODUCTS_CSV', 'data/products.csv')
    flask_app.config['STATE_FILE'] = os.getenv('STATE_FILE', 'data/state.json')
    flask_app.config['HISTORY_DB'] = os.getenv('HISTORY_DB', 'data/history.db')

    # Initialize product store (SQLite-backed)
    product_store = ProductStore(flask_app.config['HISTORY_DB'])
    flask_app.config['PRODUCT_STORE'] = product_store

    # Auto-import from CSV on first run (one-time migration)
    csv_path = flask_app.config['PRODUCTS_CSV']
    if os.path.exists(csv_path):
        imported = product_store.auto_import_csv(csv_path)
        if imported:
            logger.info("Imported %d products from %s into database", imported, csv_path)
    flask_app.config['USER_AGENT'] = os.getenv('USER_AGENT', 'Mozilla/5.0 (compatible; SaleMonitor/1.0)')
    flask_app.config['TIMEOUT'] = int(os.getenv('TIMEOUT', '30'))
    flask_app.config['MAX_RETRIES'] = int(os.getenv('MAX_RETRIES', '3'))
    flask_app.config['CONFIG_FILE'] = os.getenv('CONFIG_FILE', 'data/config.json')
    # Initial/base currency from env or config file
    initial_base_currency = os.getenv('BASE_CURRENCY') or get_base_currency(flask_app.config['CONFIG_FILE'])
    flask_app.config['BASE_CURRENCY'] = initial_base_currency.upper()
    
    # --------------- Rate Limiting ---------------
    try:
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address
        limiter = Limiter(
            get_remote_address,
            app=flask_app,
            default_limits=["60 per minute"],
            storage_uri="memory://",
        )
    except ImportError:
        limiter = None
        logger.warning("flask-limiter not installed; rate limiting disabled")

    # Conditional rate limit decorator (no-op when limiter unavailable)
    def _rate_limit(limit_string):
        def decorator(f):
            if limiter is not None:
                return limiter.limit(limit_string)(f)
            return f
        return decorator

    # --------------- CSRF Protection ---------------
    @flask_app.before_request
    def _enforce_csrf_protection():
        """Reject non-JSON POST/DELETE requests to /api/* (CSRF mitigation).

        Requests with no body are exempt (no form data to forge).
        """
        if request.method in ('POST', 'PUT', 'DELETE', 'PATCH') and request.path.startswith('/api/'):
            if request.content_length and request.content_length > 0:
                ct = request.content_type or ''
                if not ct.startswith('application/json'):
                    return jsonify({'error': 'Content-Type must be application/json'}), 400

    # --------------- Request Timing ---------------
    @flask_app.before_request
    def _start_timer():
        request._start_time = time.time()

    @flask_app.after_request
    def _log_request_duration(response):
        start = getattr(request, '_start_time', None)
        if start is not None:
            duration_ms = (time.time() - start) * 1000
            logger.info(
                "%s %s %s %.1fms",
                request.method, request.path, response.status_code, duration_ms,
            )
        return response

    # --------------- Error Sanitization Helper ---------------
    def _safe_error(e, msg='Internal server error'):
        """Log full exception server-side but return a generic message to clients."""
        logger.error("%s: %s", msg, e, exc_info=True)
        return jsonify({'error': msg}), 500

    def _build_comparison_groups(state: dict, products: list, base_currency: str) -> list:
        """Group products by shared identifiers or explicit CSV group.

        Priority: CSV group > manual group_key in state > auto-detected identifiers (mpn/sku/gtin).
        Returns a list of groups: [{group_key, source, identifiers, items: [{name,url,current_price,price_in_base,currency,last_checked}]}]
        """
        # Map url->product for names and CSV group
        name_by_url = {p.url: p.name for p in products}
        csv_group_by_url = {p.url: p.group for p in products if p.group}

        # Helper to compute price_in_base from state when available
        def _item_row(u: str, st: dict) -> dict:
            cur_price = st.get('current_price')
            cur_currency = (st.get('currency') or 'CAD').upper()
            price_in_base = st.get('price_in_base')
            return {
                'name': name_by_url.get(u, u),
                'url': u,
                'current_price': cur_price,
                'currency': cur_currency,
                'price_in_base': price_in_base if isinstance(price_in_base, (int, float)) else None,
                'last_checked': st.get('last_checked'),
            }

        groups = {}
        for url, st in state.items():
            if not isinstance(st, dict):
                continue

            # 1) Explicit CSV group (strongest)
            csv_group = csv_group_by_url.get(url)
            # 2) Manual group_key in state
            manual_key = st.get('group_key')
            # 3) Auto-detected identifiers
            ident = st.get('identifiers') or {}
            auto_key = ident.get('mpn') or ident.get('sku') or ident.get('gtin') or ident.get('gtin13')

            key = csv_group or manual_key or auto_key
            if not key:
                continue

            source = 'csv' if csv_group else ('manual' if manual_key else 'auto')
            g = groups.setdefault(key, {'group_key': key, 'source': source, 'identifiers': ident, 'items': []})
            g['items'].append(_item_row(url, st))

        # Only keep groups with at least 2 items (competitive set)
        result = [g for g in groups.values() if len(g['items']) >= 2]
        # Sort items by price_in_base when available
        for g in result:
            try:
                g['items'].sort(key=lambda x: (float(x['price_in_base']) if isinstance(x.get('price_in_base'), (int, float, str)) and str(x.get('price_in_base')).strip() != '' else float('inf')))
            except Exception:
                pass
        return result

    def _normalize_name(name: str) -> str:
        """Normalize product names for fuzzy comparison.

        Lowercase, remove punctuation, common tokens (brand noise), and collapse spaces.
        """
        import re
        if not isinstance(name, str):
            return ''
        n = name.lower()
        # Remove punctuation
        n = re.sub(r"[\-_/\\.,'\"]", " ", n)
        # Remove common noise tokens
        stop = {
            'inc', 'llc', 'ltd', 'co', 'company', 'shop', 'store', 'official',
            'bike', 'cycles', 'bikes', 'usa', 'canada', 'u.s.', 'ca',
        }
        tokens = [t for t in re.split(r"\s+", n) if t and t not in stop]
        return ' '.join(tokens).strip()

    def _similar(a: str, b: str) -> float:
        """Return similarity ratio between two strings using SequenceMatcher."""
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio()

    def _paginate(items: list) -> dict:
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

    @flask_app.route('/')
    def index():
        """Dashboard home page."""
        return render_template('index.html')
    
    @flask_app.route('/product/detail')
    def product_detail():
        """Product detail page with history chart."""
        return render_template('product_detail.html')
    
    @flask_app.route('/manage')
    def manage():
        """Product management page."""
        return render_template('manage.html')
    
    @flask_app.route('/alerts')
    def alerts():
        """Price alerts dashboard page."""
        return render_template('alerts.html')

    @flask_app.route('/compare')
    def compare():
        """Competitive comparison page."""
        return render_template('compare.html')
    
    @flask_app.route('/failures')
    def failures():
        """Failure diagnostics page."""
        return render_template('failures.html')
    
    @flask_app.route('/api/products')
    @require_api_key_for_reads
    def api_products():
        """Get all products with current state."""
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            state = load_state(flask_app.config['STATE_FILE'])
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE'])

            # Create exchange service once, reuse across all products
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            ex_service = ExchangeRateService(cache_handler=history)

            result = []
            for p in products:
                state_data = state.get(p.url, {})
                selector_source = state_data.get('selector_source', getattr(p, 'selector_source', '') or '')
                current_price = state_data.get('current_price')
                # Prefer currency from state; fallback to product default; finally CAD
                currency = (state_data.get('currency') or getattr(p, 'currency', None) or 'CAD').upper()

                # Compute price in base currency when possible
                price_in_base = None
                if current_price is not None:
                    try:
                        if currency == base_currency:
                            price_in_base = current_price
                        else:
                            converted = ex_service.convert(float(current_price), currency, base_currency)
                            price_in_base = converted if converted is not None else None
                    except (ValueError, TypeError):
                        price_in_base = None

                result.append({
                    'name': p.name,
                    'url': p.url,
                    'current_price': current_price,
                    'price_in_base': price_in_base,
                    'currency': currency,
                    'base_currency': base_currency,
                    'currency_source': state_data.get('currency_source', 'configured' if getattr(p, 'currency', None) else 'default'),
                    'target_price': p.target_price,
                    'discount_threshold': p.discount_threshold,
                    'notification_cooldown_hours': p.notification_cooldown_hours,
                    'last_checked': state_data.get('last_checked'),
                    'last_price': state_data.get('current_price'),
                    'enabled': p.enabled,
                    'selector': p.selector,
                    'selector_source': selector_source,
                    'identifiers': state_data.get('identifiers', {}),
                    'group': getattr(p, 'group', None),
                    'tags': getattr(p, 'tags', []),
                    'alert_rules': getattr(p, 'alert_rules', []),
                    'notification_channels': getattr(p, 'notification_channels', []),
                })

            return jsonify(_paginate(result))
        except (OSError, ValueError, sqlite3.Error) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/product/stats')
    @require_api_key_for_reads
    def api_product_stats():
        """Get statistics for a product."""
        try:
            url = request.args.get('url')
            group_key = request.args.get('group_key')
            if not url and not group_key:
                return jsonify({'error': 'URL or group_key parameter required'}), 400

            history = PriceHistory(flask_app.config['HISTORY_DB'])
            days = int(request.args.get('days', 30))

            # Group stats: aggregate across all URLs in the group
            if group_key:
                # Build groups and find members
                product_store = flask_app.config['PRODUCT_STORE']
                products = product_store.get_all()
                state = load_state(flask_app.config['STATE_FILE'])
                base_currency = get_base_currency(flask_app.config['CONFIG_FILE']).upper()
                groups = _build_comparison_groups(state or {}, products or [], base_currency)
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
                ex_service = ExchangeRateService(cache_handler=history)
                base_prices = []
                for (ts, price, currency) in records:
                    try:
                        if currency.upper() == base_currency:
                            base_prices.append(float(price))
                        else:
                            converted = ex_service.convert(float(price), currency.upper(), base_currency)
                            if converted is not None:
                                base_prices.append(float(converted))
                    except Exception:
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
            return _safe_error(e)

    @flask_app.route('/api/product/history')
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
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            ex_service = ExchangeRateService(cache_handler=history)
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE']).upper()
            records = []
            if group_key:
                # Build groups and aggregate histories from members
                product_store = flask_app.config['PRODUCT_STORE']
                products = product_store.get_all()
                state = load_state(flask_app.config['STATE_FILE'])
                groups = _build_comparison_groups(state or {}, products or [], base_currency)
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
                st = load_state(flask_app.config['STATE_FILE']).get(url)
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

                # Prefer stored base-currency price (recorded at check time with
                # the exchange rate that was current then). Fall back to live
                # conversion only when the stored value is missing.
                price_in_base = None
                try:
                    if stored_price_cad is not None:
                        price_in_base = float(stored_price_cad)
                    elif price is not None and currency:
                        if currency.upper() == base_currency:
                            price_in_base = price
                        else:
                            converted_base = ex_service.convert(float(price), currency.upper(), base_currency)
                            price_in_base = converted_base if converted_base is not None else None
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
                from collections import defaultdict
                day_map = defaultdict(list)
                for r in result:
                    d = r['timestamp'][:10]
                    if r.get('price_in_base') is not None:
                        day_map[d].append(r)
                collapsed = []
                for d, rs in day_map.items():
                    # pick lowest base price entry
                    rs.sort(key=lambda x: x.get('price_in_base') if x.get('price_in_base') is not None else float('inf'))
                    r = rs[0]
                    collapsed.append(r)
                # Sort by timestamp ascending
                collapsed.sort(key=lambda x: x['timestamp'])
                return jsonify(collapsed)
            else:
                return jsonify(result)
        except (OSError, ValueError, sqlite3.Error) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/product/toggle', methods=['POST'])
    @require_api_key
    def api_toggle_product():
        """Toggle product enabled status."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            product_store = flask_app.config['PRODUCT_STORE']
            product = product_store.get_by_url(url)
            if not product:
                return jsonify({'error': 'Product not found'}), 404
            
            new_enabled = not product.enabled
            product_store.update(url, enabled=new_enabled)
            
            return jsonify({'success': True, 'enabled': new_enabled})
        except (OSError, ValueError) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/product/check', methods=['POST'])
    @require_api_key
    def api_check_product():
        """Manually trigger price check for a product."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            # Find product
            product_store = flask_app.config['PRODUCT_STORE']
            product = product_store.get_by_url(url)
            
            if not product:
                return jsonify({'error': 'Product not found'}), 404
            
            # Extract price
            extractor = PriceExtractor(
                user_agent=flask_app.config['USER_AGENT'],
                timeout=flask_app.config['TIMEOUT'],
                max_retries=flask_app.config['MAX_RETRIES']
            )
            price, selector_source, detected_currency = extractor.extract_price_with_currency(product.url, product.selector, default_currency=product.currency)
            
            if price is None:
                # Record failure in history for alerts tracking
                history = PriceHistory(flask_app.config['HISTORY_DB'])
                history.record_price(product.url, product.name, None, status='failed', currency=product.currency or 'CAD')
                return jsonify({'error': 'Failed to extract price'}), 500
            
            # Choose currency with preference for detected when available (configurable)
            prefer_detected = os.getenv('PREFER_DETECTED_CURRENCY', '1').strip().lower() not in ('0','false','no')
            currency_source = 'default'
            if prefer_detected and detected_currency:
                currency = detected_currency
                currency_source = 'detected'
            elif getattr(product, 'currency', None):
                currency = product.currency
                currency_source = 'configured'
            else:
                currency = detected_currency or 'CAD'
                currency_source = 'detected' if detected_currency else 'default'
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE'])
            price_in_base = None
            if currency == base_currency:
                price_in_base = price
            else:
                history = PriceHistory(flask_app.config['HISTORY_DB'])
                ex_service = ExchangeRateService(cache_handler=history)
                converted = ex_service.convert(float(price), currency, base_currency)
                price_in_base = converted if converted is not None else None

            # Update state
            state = load_state(flask_app.config['STATE_FILE'])
            existing_state = state.get(url, {})
            new_identifiers = getattr(extractor, 'last_identifiers', {}) or {}
            # Preserve existing identifiers and group_key if new extraction didn't find any
            preserved_identifiers = existing_state.get('identifiers', {})
            merged_identifiers = {**preserved_identifiers, **new_identifiers} if new_identifiers else preserved_identifiers
            
            state[url] = {
                'current_price': price,
                'last_checked': datetime.now(timezone.utc).isoformat(),
                'last_price': existing_state.get('current_price', price),
                'selector_source': selector_source,
                'currency': currency,
                'currency_source': currency_source,
                'price_in_base': price_in_base,
                'identifiers': merged_identifiers,
                'group_key': existing_state.get('group_key')  # Preserve manual group_key
            }
            save_state(flask_app.config['STATE_FILE'], state)
            
            # Record in history (count as success so stats include manual checks)
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            history.record_price(product.url, product.name, price, status='success', currency=currency, price_cad=price_in_base)
            
            return jsonify({
                'success': True,
                'price': price,
                'price_in_base': price_in_base,
                'currency': currency,
                'base_currency': base_currency,
                'timestamp': state[url]['last_checked'],
                'selector_source': selector_source,
                'currency_source': currency_source
            })
        except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
            return _safe_error(e)

    @flask_app.route('/api/products/check-all', methods=['POST'])
    @require_api_key
    @_rate_limit("2 per minute")
    def api_check_all_products():
        """Trigger price check for all enabled products.

        Returns summary of successes / failures.
        """
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            enabled = product_store.get_enabled()
            if not enabled:
                return jsonify({'success': True, 'updated': 0, 'failed': 0, 'message': 'No enabled products'}), 200

            state = load_state(flask_app.config['STATE_FILE'])
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            ex_service = ExchangeRateService(cache_handler=history)
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE'])

            extractor = PriceExtractor(
                user_agent=flask_app.config['USER_AGENT'],
                timeout=flask_app.config['TIMEOUT'],
                max_retries=flask_app.config['MAX_RETRIES']
            )

            updated = 0
            failed = 0
            for p in enabled:
                try:
                    product_url = (p.url or '').strip()
                    if not product_url or not product_url.lower().startswith(('http://','https://')):
                        # Invalid URL format; count as failed and skip
                        history.record_price(p.url or 'invalid', p.name, None, status='failed', currency=p.currency or 'CAD')
                        failed += 1
                        continue
                    price, selector_source, detected_currency = extractor.extract_price_with_currency(product_url, p.selector, default_currency=p.currency)
                    if price is None:
                        # Record failure in history for alerts tracking
                        history.record_price(p.url, p.name, None, status='failed', currency=p.currency or 'CAD')
                        failed += 1
                        continue
                    # Choose currency with preference for detected when available (configurable)
                    prefer_detected = os.getenv('PREFER_DETECTED_CURRENCY', '1').strip().lower() not in ('0','false','no')
                    if prefer_detected and detected_currency:
                        currency = detected_currency
                        currency_source = 'detected'
                    elif getattr(p, 'currency', None):
                        currency = p.currency
                        currency_source = 'configured'
                    else:
                        currency = detected_currency or 'CAD'
                        currency_source = 'detected' if detected_currency else 'default'
                    if currency == base_currency:
                        price_in_base = price
                    else:
                        converted = ex_service.convert(float(price), currency, base_currency)
                        price_in_base = converted if converted is not None else None

                    prev_state = state.get(p.url, {})
                    state[p.url] = {
                        'current_price': price,
                        'last_checked': datetime.now(timezone.utc).isoformat(),
                        'last_price': prev_state.get('current_price', price),
                        'selector_source': selector_source or prev_state.get('selector_source'),
                        'currency': currency,
                        'currency_source': currency_source,
                        'price_in_base': price_in_base,
                        'identifiers': getattr(extractor, 'last_identifiers', {}) or prev_state.get('identifiers')
                    }
                    history.record_price(p.url, p.name, price, status='success', currency=currency, price_cad=price_in_base)
                    updated += 1
                except (requests.exceptions.RequestException, ValueError, sqlite3.Error):
                    # Record failure in history for alerts tracking
                    history.record_price(p.url, p.name, None, status='failed', currency=getattr(p, 'currency', None) or 'CAD')
                    failed += 1
                    continue

            save_state(flask_app.config['STATE_FILE'], state)
            return jsonify({
                'success': True,
                'updated': updated,
                'failed': failed,
                'base_currency': base_currency,
                'timestamp': datetime.now(timezone.utc).isoformat()
            })
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/config', methods=['GET'])
    @require_api_key_for_reads
    def api_get_config():
        cfg = load_config(flask_app.config['CONFIG_FILE'])
        return jsonify({'base_currency': cfg.get('base_currency', 'CAD')})

    @flask_app.route('/api/config/base-currency', methods=['POST'])
    @require_api_key
    def api_set_base_currency():
        try:
            data = request.get_json() or {}
            new_cur = str(data.get('base_currency', '')).upper().strip()
            if not new_cur or len(new_cur) != 3 or not new_cur.isalpha():
                return jsonify({'error': 'Invalid currency code'}), 400
            # Persist
            cfg = load_config(flask_app.config['CONFIG_FILE'])
            cfg['base_currency'] = new_cur
            save_config(flask_app.config['CONFIG_FILE'], cfg)
            flask_app.config['BASE_CURRENCY'] = new_cur
            return jsonify({'success': True, 'base_currency': new_cur})
        except (OSError, ValueError) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/product/delete', methods=['POST'])
    @require_api_key
    @_rate_limit("10 per minute")
    def api_delete_product():
        """Delete a product."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            product_store = flask_app.config['PRODUCT_STORE']
            if not product_store.delete(url):
                return jsonify({'error': 'Product not found'}), 404
            
            return jsonify({'success': True})
        except (OSError, ValueError) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/products/auto-detect-all', methods=['POST'])
    @require_api_key
    @_rate_limit("2 per minute")
    def api_auto_detect_all():
        """Attempt to auto-detect price selectors for all products."""
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            
            extractor = PriceExtractor(
                user_agent=flask_app.config['USER_AGENT'],
                timeout=flask_app.config['TIMEOUT'],
                max_retries=flask_app.config['MAX_RETRIES']
            )
            
            successful = 0
            failed = 0
            
            for product in products:
                try:
                    # Try to extract price with empty selector to force auto-detection
                    price, selector_source = extractor.extract_price(product.url, "")
                    
                    if price is not None and selector_source == 'auto':
                        # Auto-detection succeeded - clear selector and mark as auto
                        product_store.update(product.url, selector='', selector_source='auto')
                        successful += 1
                    else:
                        failed += 1
                except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
                    logging.error("Auto-detect failed for %s: %s", product.url, e)
                    failed += 1
            
            return jsonify({
                'success': True,
                'successful': successful,
                'failed': failed
            })
        except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
            logging.error("Bulk auto-detect error: %s", e)
            return _safe_error(e)
    
    @flask_app.route('/api/product/add', methods=['POST'])
    @require_api_key
    def api_add_product():
        """Add a new product."""
        try:
            data = request.get_json()
            
            # Validate required fields (selector is now optional)
            required = ['name', 'url']
            for field in required:
                if not data.get(field):
                    return jsonify({'error': f'{field} is required'}), 400

            # Validate URL scheme
            parsed_url = urlparse(data['url'])
            if parsed_url.scheme not in ('http', 'https'):
                return jsonify({'error': 'URL must use http or https'}), 400

            # Validate name length
            if len(data['name']) > 500:
                return jsonify({'error': 'Name must be 500 characters or fewer'}), 400
            
            # Parse and validate optional numeric fields
            def _parse_float(val, field_name):
                if val in (None, ''):
                    return None
                try:
                    result = float(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f'{field_name} must be a valid number') from exc
                if result < 0:
                    raise ValueError(f'{field_name} must not be negative')
                return result

            def _parse_int(val, field_name, default=None):
                if val in (None, ''):
                    return default
                try:
                    parsed = int(val)
                except (TypeError, ValueError) as e:
                    raise ValueError(f'{field_name} must be a valid positive integer') from e
                if parsed < 0:
                    raise ValueError(f'{field_name} must be a positive number')
                if parsed > 8760:
                    raise ValueError(f'{field_name} must be 8760 or fewer')
                return parsed

            try:
                target_price = _parse_float(data.get('target_price'), 'target_price')
                discount_threshold = _parse_float(data.get('discount_threshold'), 'discount_threshold')
                cooldown_hours = _parse_int(data.get('notification_cooldown_hours'), 'notification_cooldown_hours', default=24)
            except ValueError as ve:
                return jsonify({'error': str(ve)}), 400

            # Parse list fields
            def _parse_csv_list(val):
                if not val:
                    return []
                if isinstance(val, list):
                    return [s.strip() for s in val if isinstance(s, str) and s.strip()]
                return [s.strip() for s in str(val).split(',') if s.strip()]

            # Create product
            new_product = Product(
                name=data['name'],
                url=data['url'],
                target_price=target_price,
                discount_threshold=discount_threshold,
                selector=data.get('selector', ''),  # Default to empty string if not provided
                enabled=data.get('enabled', True),
                notification_cooldown_hours=cooldown_hours,
                group=data.get('group', '').strip() or None,
                tags=_parse_csv_list(data.get('tags')),
                alert_rules=_parse_csv_list(data.get('alert_rules')),
                notification_channels=_parse_csv_list(data.get('notification_channels')),
            )
            
            # Check for duplicate URL and add
            product_store = flask_app.config['PRODUCT_STORE']
            if product_store.get_by_url(new_product.url):
                return jsonify({'error': 'Product with this URL already exists'}), 400
            
            product_store.add(new_product)
            
            # Auto-check price for newly added product
            try:
                extractor = PriceExtractor(
                    user_agent=flask_app.config['USER_AGENT'],
                    timeout=flask_app.config['TIMEOUT'],
                    max_retries=flask_app.config['MAX_RETRIES']
                )
                price, selector_source, detected_currency = extractor.extract_price_with_currency(
                    new_product.url, 
                    new_product.selector, 
                    default_currency=getattr(new_product, 'currency', 'CAD')
                )
                
                if price is not None:
                    # Determine currency
                    prefer_detected = os.getenv('PREFER_DETECTED_CURRENCY', '1').strip().lower() not in ('0','false','no')
                    currency_source = 'default'
                    if prefer_detected and detected_currency:
                        currency = detected_currency
                        currency_source = 'detected'
                    elif getattr(new_product, 'currency', None):
                        currency = new_product.currency
                        currency_source = 'configured'
                    else:
                        currency = detected_currency or 'CAD'
                        currency_source = 'detected' if detected_currency else 'default'
                    
                    # Convert to base currency if needed
                    base_currency = get_base_currency(flask_app.config['CONFIG_FILE'])
                    price_in_base = None
                    if currency == base_currency:
                        price_in_base = price
                    else:
                        history = PriceHistory(flask_app.config['HISTORY_DB'])
                        ex_service = ExchangeRateService(cache_handler=history)
                        converted = ex_service.convert(float(price), currency, base_currency)
                        price_in_base = converted if converted is not None else None
                    
                    # Update state
                    state = load_state(flask_app.config['STATE_FILE'])
                    state[new_product.url] = {
                        'name': new_product.name,
                        'url': new_product.url,
                        'current_price': price,
                        'last_checked': datetime.now(timezone.utc).isoformat(),
                        'last_price': price,
                        'selector': new_product.selector,
                        'selector_source': selector_source,
                        'currency': currency,
                        'currency_source': currency_source,
                        'price_in_base': price_in_base
                    }
                    save_state(flask_app.config['STATE_FILE'], state)
                    
                    # Record in history
                    history = PriceHistory(flask_app.config['HISTORY_DB'])
                    history.record_price(new_product.url, new_product.name, price, status='success', currency=currency, price_cad=price_in_base)
                    
                    logging.info(f"Auto-checked new product '{new_product.name}': ${price} {currency}")
                else:
                    logging.warning(f"Failed to auto-check price for new product '{new_product.name}'")
            except Exception as e:
                # Don't fail the whole request if price check fails
                logging.error(f"Error auto-checking price for new product '{new_product.name}': {e}")
            
            return jsonify({'success': True, 'product': {
                'name': new_product.name,
                'url': new_product.url,
                'enabled': new_product.enabled,
                'notification_cooldown_hours': new_product.notification_cooldown_hours
            }})
        except (OSError, ValueError) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/product/update', methods=['POST'])
    @require_api_key
    def api_update_product():
        """Update an existing product."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            product_store = flask_app.config['PRODUCT_STORE']
            p = product_store.get_by_url(url)
            if not p:
                return jsonify({'error': 'Product not found'}), 404

            # Safe parsing helpers with validation
            def _parse_float(val, current, field_name):
                if val in (None, ''):
                    return current
                try:
                    return float(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f'{field_name} must be a valid number') from exc

            def _parse_int(val, current, field_name):
                if val in (None, ''):
                    return current
                try:
                    parsed = int(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f'{field_name} must be a valid positive integer') from exc
                if parsed < 0:
                    raise ValueError(f'{field_name} must be a positive number')
                return parsed

            try:
                target_price = _parse_float(data.get('target_price'), p.target_price, 'target_price')
                discount_threshold = _parse_float(data.get('discount_threshold'), p.discount_threshold, 'discount_threshold')
                cooldown_hours = _parse_int(data.get('notification_cooldown_hours'), p.notification_cooldown_hours, 'notification_cooldown_hours')
            except ValueError as ve:
                return jsonify({'error': str(ve)}), 400

            def _parse_csv_list(val, current):
                if val is None:
                    return current
                if isinstance(val, list):
                    return [s.strip() for s in val if isinstance(s, str) and s.strip()]
                return [s.strip() for s in str(val).split(',') if s.strip()]

            fields = {
                'name': data.get('name', p.name),
                'target_price': target_price,
                'discount_threshold': discount_threshold,
                'selector': data.get('selector', p.selector),
                'enabled': data.get('enabled', p.enabled),
                'notification_cooldown_hours': cooldown_hours,
                'group': data.get('group', p.group).strip() if data.get('group') is not None else p.group,
                'tags': _parse_csv_list(data.get('tags'), getattr(p, 'tags', [])),
                'alert_rules': _parse_csv_list(data.get('alert_rules'), getattr(p, 'alert_rules', [])),
                'notification_channels': _parse_csv_list(data.get('notification_channels'), getattr(p, 'notification_channels', [])),
            }
            product_store.update(url, **fields)

            updated = product_store.get_by_url(url)
            return jsonify({'success': True, 'product': {
                'name': updated.name,
                'url': updated.url,
                'enabled': updated.enabled,
                'notification_cooldown_hours': updated.notification_cooldown_hours,
                'target_price': updated.target_price,
                'discount_threshold': updated.discount_threshold,
                'selector': updated.selector,
                'group': updated.group
            }})
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/products/bulk-import', methods=['POST'])
    @require_api_key
    def api_bulk_import():
        """Bulk import products from JSON array."""
        try:
            data = request.get_json()
            items = data.get('products', [])
            if not isinstance(items, list) or not items:
                return jsonify({'error': 'products must be a non-empty array'}), 400
            if len(items) > 100:
                return jsonify({'error': 'Maximum 100 products per import'}), 400

            product_store = flask_app.config['PRODUCT_STORE']
            existing_urls = product_store.urls()

            def _parse_csv_list(val):
                if not val:
                    return []
                if isinstance(val, list):
                    return [s.strip() for s in val if isinstance(s, str) and s.strip()]
                return [s.strip() for s in str(val).split(',') if s.strip()]

            added = []
            skipped = []
            errors = []

            for idx, item in enumerate(items):
                row_label = item.get('name') or f'row {idx + 1}'
                name = (item.get('name') or '').strip()
                url = (item.get('url') or '').strip()

                if not name or not url:
                    errors.append(f'{row_label}: name and url are required')
                    continue

                parsed_url = urlparse(url)
                if parsed_url.scheme not in ('http', 'https'):
                    errors.append(f'{row_label}: URL must use http or https')
                    continue

                if len(name) > 500:
                    errors.append(f'{row_label}: name too long')
                    continue

                if url in existing_urls:
                    skipped.append(name)
                    continue

                try:
                    tp = float(item['target_price']) if item.get('target_price') not in (None, '') else None
                    dt = float(item['discount_threshold']) if item.get('discount_threshold') not in (None, '') else None
                except (TypeError, ValueError):
                    errors.append(f'{row_label}: invalid numeric value')
                    continue

                new_product = Product(
                    name=name,
                    url=url,
                    target_price=tp,
                    discount_threshold=dt,
                    selector=item.get('selector', ''),
                    enabled=item.get('enabled', True),
                    notification_cooldown_hours=int(item.get('notification_cooldown_hours', 24)),
                    group=(item.get('group') or '').strip() or None,
                    tags=_parse_csv_list(item.get('tags')),
                    alert_rules=_parse_csv_list(item.get('alert_rules')),
                    notification_channels=_parse_csv_list(item.get('notification_channels')),
                )
                product_store.add(new_product)
                existing_urls.add(url)
                added.append(name)

            return jsonify({
                'success': True,
                'added': len(added),
                'skipped': len(skipped),
                'errors': errors,
                'added_names': added,
                'skipped_names': skipped,
            })
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/alerts')
    @require_api_key_for_reads
    def api_alerts():
        """Get products that have hit their price targets or discount thresholds."""
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            state = load_state(flask_app.config['STATE_FILE'])
            history = PriceHistory(flask_app.config['HISTORY_DB'])

            alerts = []
            failure_threshold = float(os.getenv('ALERT_FAILURE_THRESHOLD', '50'))  # 50% failure rate
            min_checks = int(os.getenv('ALERT_MIN_CHECKS', '3'))  # Minimum 3 checks to report
            
            for p in products:
                if not p.enabled:
                    continue

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

                    # Check discount threshold - use historical max price from DB
                    elif p.discount_threshold:
                        # Get historical stats to find the highest price in last 30 days
                        stats = history.get_stats(p.url, days=30)
                        max_price = stats.get('max_price')
                        
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
                
                # Check for high failure rate (last 7 days)
                try:
                    failure_stats = history.get_failure_stats(p.url, days=7)
                    if (failure_stats['total_checks'] >= min_checks and 
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
                except (sqlite3.Error, KeyError):
                    # Skip failure check if history unavailable
                    pass

            # Optional competitive alerts (gated by env to avoid test disruption)
            enable_competitive = os.getenv('ENABLE_COMPETITIVE_ALERTS', '0').strip().lower() in ('1','true','yes')
            if enable_competitive:
                try:
                    base_currency = get_base_currency(flask_app.config['CONFIG_FILE']).upper()
                    groups = _build_comparison_groups(state, products, base_currency)
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
                        except Exception:
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
                except Exception:
                    pass

            return jsonify(alerts)
        except (OSError, ValueError, sqlite3.Error) as e:
            return _safe_error(e)

    @flask_app.route('/api/compare/groups')
    @require_api_key_for_reads
    def api_compare_groups():
        """Return competitive groups built from identifiers in state."""
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            state = load_state(flask_app.config['STATE_FILE'])
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE']).upper()
            try:
                groups = _build_comparison_groups(state or {}, products or [], base_currency)
                return jsonify(_paginate(groups))
            except Exception as e:
                logging.error("Comparison group build failed: %s", e)
                return jsonify(_paginate([]))
        except (OSError, ValueError, sqlite3.Error) as e:
            return _safe_error(e)

    @flask_app.route('/api/compare/backfill-identifiers', methods=['POST'])
    @require_api_key
    @_rate_limit("2 per minute")
    def api_compare_backfill_identifiers():
        """Backfill product identifiers in state by scraping pages.

        Returns {updated: n, failed: m} and does not modify prices.
        """
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            state = load_state(flask_app.config['STATE_FILE'])
            extractor = PriceExtractor(
                user_agent=flask_app.config['USER_AGENT'],
                timeout=flask_app.config['TIMEOUT'],
                max_retries=flask_app.config['MAX_RETRIES']
            )
            updated = 0
            failed = 0
            for p in products:
                try:
                    idents = extractor.extract_identifiers(p.url)
                    if idents:
                        prev = state.get(p.url, {})
                        prev['identifiers'] = idents
                        # Preserve existing fields
                        state[p.url] = prev
                        updated += 1
                    else:
                        failed += 1
                except Exception:
                    failed += 1
            save_state(flask_app.config['STATE_FILE'], state)
            return jsonify({'success': True, 'updated': updated, 'failed': failed})
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/compare/suggest')
    @require_api_key_for_reads
    def api_compare_suggest():
        """Suggest likely competitor pairs based on fuzzy name matching.

        Returns list of suggestions: [{nameA, urlA, nameB, urlB, similarity}]
        """
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            # Build normalized names
            items = []
            for p in products:
                items.append({'url': p.url, 'name': p.name, 'norm': _normalize_name(p.name)})
            suggestions = []
            threshold = float(os.getenv('COMPARE_NAME_SIMILARITY', '0.88'))
            # Compare pairs
            for i in range(len(items)):
                for j in range(i+1, len(items)):
                    a, b = items[i], items[j]
                    sim = _similar(a['norm'], b['norm'])
                    if sim >= threshold:
                        suggestions.append({
                            'nameA': a['name'], 'urlA': a['url'],
                            'nameB': b['name'], 'urlB': b['url'],
                            'similarity': round(sim, 3)
                        })
            # Sort by similarity desc
            suggestions.sort(key=lambda s: s['similarity'], reverse=True)
            return jsonify(_paginate(suggestions))
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/compare/link', methods=['POST'])
    @require_api_key
    def api_compare_link():
        """Link two product URLs under a shared manual group_key.

        Body: { urlA, urlB, group_key? }
        """
        try:
            data = request.get_json() or {}
            urlA = (data.get('urlA') or '').strip()
            urlB = (data.get('urlB') or '').strip()
            group_key = (data.get('group_key') or '').strip()
            if not urlA or not urlB:
                return jsonify({'error': 'urlA and urlB required'}), 400
            # Generate group key if absent
            if not group_key:
                # Use normalized name pair hash for stability
                import hashlib
                pair = '|'.join(sorted([urlA, urlB]))
                group_key = 'manual:' + hashlib.sha1(pair.encode('utf-8')).hexdigest()[:8]
            state = load_state(flask_app.config['STATE_FILE'])
            a = state.get(urlA, {})
            b = state.get(urlB, {})
            a['group_key'] = group_key
            b['group_key'] = group_key
            state[urlA] = a
            state[urlB] = b
            save_state(flask_app.config['STATE_FILE'], state)
            return jsonify({'success': True, 'group_key': group_key})
        except (OSError, ValueError) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/failures')
    @require_api_key_for_reads
    def api_failures():
        """Get detailed failure information for all products."""
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            state = load_state(flask_app.config['STATE_FILE'])
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            
            days = int(request.args.get('days', 7))
            
            failures = []
            for p in products:
                try:
                    failure_stats = history.get_failure_stats(p.url, days=days)
                    state_data = state.get(p.url, {})
                    
                    # Include products with at least one failure
                    if failure_stats['failed_checks'] > 0:
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
                except (sqlite3.Error, KeyError):
                    # Skip if history unavailable for this product
                    continue
            
            # Sort by failure rate descending, then by failed count
            failures.sort(key=lambda x: (x['failure_rate'], x['failed_checks']), reverse=True)
            
            return jsonify(_paginate(failures))
        except (OSError, ValueError, sqlite3.Error) as e:
            return _safe_error(e)
    
    @flask_app.route('/api/export/history')
    @require_api_key_for_reads
    def api_export_history():
        """Export all price history as CSV."""
        try:
            from io import StringIO

            # Legacy export format to maintain backward compatibility with tests
            with sqlite3.connect(flask_app.config['HISTORY_DB']) as conn:
                cursor = conn.execute(
                    """
                    SELECT product_name, product_url, price, timestamp, check_status
                    FROM price_history
                    ORDER BY timestamp DESC
                    """
                )
                output = StringIO()
                writer = __import__('csv').writer(output)
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
            return _safe_error(e)

    @flask_app.route('/api/export/products')
    @require_api_key_for_reads
    def api_export_products():
        """Export all products as CSV."""
        try:
            from io import StringIO
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
            output = StringIO()
            writer = __import__('csv').writer(output)
            from sale_monitor.storage.csv_products import CSV_COLUMNS
            writer.writerow(CSV_COLUMNS)
            for p in products:
                writer.writerow([
                    p.name, p.url,
                    p.target_price if p.target_price is not None else '',
                    p.discount_threshold if p.discount_threshold is not None else '',
                    p.selector,
                    'true' if p.enabled else 'false',
                    p.notification_cooldown_hours,
                    p.selector_source or '',
                    getattr(p, 'currency', 'CAD'),
                    getattr(p, 'group', '') or '',
                    ','.join(getattr(p, 'tags', []) or []),
                    ','.join(getattr(p, 'alert_rules', []) or []),
                    ','.join(getattr(p, 'notification_channels', []) or []),
                ])
            output.seek(0)
            return Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={
                    'Content-Disposition': f'attachment; filename=products_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'
                }
            )
        except (OSError, sqlite3.Error) as e:
            return _safe_error(e)

    @flask_app.route('/api/history/all')
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
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            ex_service = ExchangeRateService(cache_handler=history)
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE']).upper()
            # Prefer names from products DB
            try:
                product_store = flask_app.config['PRODUCT_STORE']
                current_products = product_store.get_all()
                name_by_url = {p.url: p.name for p in current_products}
            except (OSError, ValueError):
                name_by_url = {}

            # Restrict to current product URLs when available
            url_filter = set(name_by_url.keys()) if name_by_url else None

            # Single bulk query for all products' history
            all_history = history.get_all_history_extended(days=days, url_filter=url_filter)

            # Always downsample to one record per product per day (latest
            # successful) to keep payloads manageable.  With frequent checks
            # (~every 2 min) the raw data can be thousands of rows per product
            # per week, most with identical prices.
            downsample = True

            result = []
            # If DB has no history but we have products, try state fallback
            urls_to_process = set(all_history.keys())
            if name_by_url:
                urls_to_process |= set(name_by_url.keys())

            for url in urls_to_process:
                if name_by_url and url not in name_by_url:
                    continue
                records = all_history.get(url)
                if not records:
                    # Fallback: synthesize a single point from current state
                    try:
                        st = load_state(flask_app.config['STATE_FILE']).get(url)
                        if st and 'current_price' in st:
                            cur = st.get('current_price')
                            cur_currency = (st.get('currency') or 'CAD').upper()
                            records = [
                                (st.get('last_checked') or datetime.now(timezone.utc).isoformat(), cur, 'success', cur_currency, None)
                            ]
                        else:
                            continue
                    except (ValueError, TypeError, sqlite3.Error, requests.exceptions.RequestException):
                        continue
                # Choose display name: prefer CSV; else DB name from records
                display_name = name_by_url.get(url, url)
                try:
                    if display_name is not None and str(display_name).strip() != "":
                        _ = float(str(display_name))
                        display_name = name_by_url.get(url, url)
                except ValueError:
                    pass

                # Build series, optionally downsampling to one point per day
                if downsample:
                    # Keep latest success per day
                    day_best = {}
                    for (ts, price, status, currency, stored_cad) in records:
                        if status != 'success' or price is None:
                            continue
                        day = ts[:10]
                        if day not in day_best:
                            day_best[day] = (ts, price, currency, stored_cad)
                    day_records = sorted(day_best.values(), key=lambda r: r[0])
                else:
                    day_records = [
                        (ts, price, currency, stored_cad)
                        for (ts, price, status, currency, stored_cad) in records
                        if status == 'success' and price is not None
                    ]

                series = []
                for (ts, price, currency, stored_cad) in day_records:
                    # Prefer stored base-currency price (recorded at check time).
                    # Fall back to live conversion for legacy records.
                    price_in_base = None
                    try:
                        if stored_cad is not None:
                            price_in_base = float(stored_cad)
                        elif currency:
                            if currency.upper() == base_currency:
                                price_in_base = price
                            else:
                                converted_base = ex_service.convert(float(price), currency.upper(), base_currency)
                                price_in_base = converted_base if converted_base is not None else None
                    except (ValueError, TypeError):
                        pass

                    try:
                        if price_in_base is not None:
                            price_in_base = round(float(price_in_base), 2)
                    except (TypeError, ValueError):
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

            return jsonify(_paginate(result))
        except (OSError, ValueError, sqlite3.Error) as e:
            return _safe_error(e)

    # Track application start time for uptime reporting
    _app_start_time = time.time()

    @flask_app.route('/api/health')
    @require_api_key_for_reads
    def api_health():
        """Lightweight health check for Docker/uptime probes."""
        try:
            with sqlite3.connect(flask_app.config['HISTORY_DB']) as conn:
                conn.execute("SELECT 1 FROM price_history LIMIT 1")
            return jsonify({'status': 'ok'})
        except Exception:
            return jsonify({'status': 'error'}), 500

    @flask_app.route('/api/health/detailed')
    @require_api_key_for_reads
    def api_health_detailed():
        """Detailed health check: DB integrity, product counts, uptime, cache age."""
        try:
            # DB integrity + row count
            db_ok = False
            rows = 0
            try:
                with sqlite3.connect(flask_app.config['HISTORY_DB']) as conn:
                    ok_row = conn.execute("PRAGMA integrity_check").fetchone()
                    db_ok = bool(ok_row and ok_row[0] == 'ok')
                    row = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()
                    rows = int(row[0]) if row else 0
            except sqlite3.Error:
                pass

            # DB file size
            db_size_mb = None
            try:
                db_size_mb = round(os.path.getsize(flask_app.config['HISTORY_DB']) / (1024 * 1024), 2)
            except OSError:
                pass

            # Product counts
            product_count = 0
            enabled_count = 0
            try:
                product_store = flask_app.config['PRODUCT_STORE']
                products = product_store.get_all()
                product_count = len(products)
                enabled_count = sum(1 for p in products if p.enabled)
            except (OSError, ValueError):
                pass

            # Last check time from state
            last_check = None
            try:
                state = load_state(flask_app.config['STATE_FILE'])
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
                with sqlite3.connect(flask_app.config['HISTORY_DB']) as conn:
                    er_row = conn.execute(
                        "SELECT MAX(timestamp) FROM exchange_rates"
                    ).fetchone()
                    if er_row and er_row[0]:
                        from datetime import datetime as _dt
                        cached_at = _dt.fromisoformat(er_row[0].replace('Z', '+00:00'))
                        age_seconds = (datetime.now(timezone.utc) - cached_at.replace(tzinfo=timezone.utc if cached_at.tzinfo is None else cached_at.tzinfo)).total_seconds()
                        exchange_rate_age = round(age_seconds)
            except (sqlite3.Error, ValueError, TypeError):
                pass

            uptime_seconds = round(time.time() - _app_start_time)

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

    # ---------------- Settings / Notification Config -----------------

    @flask_app.route('/settings')
    def settings_page():
        return render_template('settings.html')

    @flask_app.route('/api/settings/notifications')
    @require_api_key_for_reads
    def api_get_notification_settings():
        """Return notification config with password masked."""
        try:
            cfg = load_notification_config(flask_app.config['CONFIG_FILE'])
            # Mask SMTP password
            smtp = cfg.get('smtp', {})
            if smtp.get('password'):
                smtp['password'] = '********'
            return jsonify(cfg)
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/settings/notifications', methods=['POST'])
    @_rate_limit("10 per minute")
    @require_api_key
    def api_save_notification_settings():
        """Save notification config. Accepts full notifications object."""
        try:
            data = request.get_json(force=True)
            if not isinstance(data, dict):
                return jsonify({'error': 'Expected JSON object'}), 400
            # If password is masked placeholder, preserve the existing one
            existing = load_notification_config(flask_app.config['CONFIG_FILE'])
            smtp = data.get('smtp', {})
            if isinstance(smtp, dict) and smtp.get('password') == '********':
                smtp['password'] = existing.get('smtp', {}).get('password', '')
            save_notification_config(flask_app.config['CONFIG_FILE'], data)
            return jsonify({'status': 'saved'})
        except (OSError, ValueError) as e:
            return _safe_error(e)

    @flask_app.route('/api/settings/notifications/test', methods=['POST'])
    @_rate_limit("5 per minute")
    @require_api_key
    def api_test_notification():
        """Send a test notification to a specified channel."""
        try:
            data = request.get_json(force=True)
            channel = data.get('channel', 'smtp')

            if channel == 'smtp':
                cfg = load_notification_config(flask_app.config['CONFIG_FILE'])
                smtp = cfg.get('smtp', {})
                if not smtp.get('server') or not smtp.get('to_email'):
                    return jsonify({'error': 'SMTP not configured'}), 400
                from sale_monitor.services.notifications import NotificationManager, SmtpConfig
                smtp_cfg = SmtpConfig(
                    server=smtp['server'], port=int(smtp.get('port', 587)),
                    username=smtp.get('username', ''), password=smtp.get('password', ''),
                    from_email=smtp.get('from_email', ''), to_email=smtp['to_email'],
                    enable=True, use_starttls=smtp.get('use_starttls', True),
                )
                mgr = NotificationManager(smtp_cfg)
                mgr.send_sale_notification(
                    product_name='Test Product', product_url='https://example.com',
                    current_price=29.99, currency='CAD', triggered_by='test',
                )
                return jsonify({'status': 'sent', 'channel': 'smtp'})
            else:
                # Webhook test — find matching webhook by name or type
                cfg = load_notification_config(flask_app.config['CONFIG_FILE'])
                webhooks = cfg.get('webhooks', [])
                target_wh = None
                for wh in webhooks:
                    if wh.get('name') == channel or wh.get('type') == channel:
                        target_wh = wh
                        break
                if not target_wh or not target_wh.get('url'):
                    return jsonify({'error': f'Webhook "{channel}" not found or has no URL'}), 400
                from sale_monitor.services.webhooks import DiscordWebhookNotifier, SlackWebhookNotifier
                wh_type = target_wh.get('type', '').lower()
                if wh_type == 'discord':
                    notifier = DiscordWebhookNotifier(name=channel, url=target_wh['url'])
                elif wh_type == 'slack':
                    notifier = SlackWebhookNotifier(name=channel, url=target_wh['url'])
                else:
                    return jsonify({'error': f'Unknown webhook type: {wh_type}'}), 400
                ok = notifier.send_test()
                if ok:
                    return jsonify({'status': 'sent', 'channel': channel})
                return jsonify({'error': f'Webhook "{channel}" failed to send'}), 502
        except Exception as e:
            logger.error("Test notification failed: %s", e, exc_info=True)
            return _safe_error(e)

    # ---------------- Product Image Endpoint (added) -----------------
    # Simple in-memory image cache: {url: {image_url: str, fetched: datetime}}
    _IMAGE_CACHE = {}

    def _is_public_url(url: str) -> bool:
        """Basic SSRF guard: allow only http/https and public IPs/hosts."""
        import socket
        import ipaddress

        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = (parsed.hostname or "").strip()
        if not hostname:
            return False
        # Disallow obvious local names
        forbidden_hosts = {"localhost", "localhost.localdomain"}
        if hostname in forbidden_hosts:
            return False
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False
        if not infos:
            return False
        for rec in infos:
            family = rec[0]
            try:
                if family == socket.AF_INET:
                    ip = ipaddress.ip_address(rec[4][0])
                elif hasattr(socket, 'AF_INET6') and family == socket.AF_INET6:
                    ip = ipaddress.ip_address(rec[4][0])
                else:
                    # Unknown family: reject
                    return False
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                ):
                    return False
            except (ValueError, OSError):
                return False
        return True

    # Set Pillow decompression bomb limit
    try:
        from PIL import Image as _PILImage
        _PILImage.MAX_IMAGE_PIXELS = 25_000_000  # ~5000x5000
    except ImportError:
        pass

    def _extract_image_url(html: str, base_url: str) -> str | None:
        """Extract representative product image URL from HTML.
        Preference order:
        1) Amazon-specific image extraction
        2) JSON-LD Product image
        3) OpenGraph/Twitter card image
        4) <img> with explicit product-ish classes/attributes (data-zoom-image, srcset largest)
        """
        import re
        import json as _json
        from urllib.parse import urlparse, urljoin

        def _abs(u: str) -> str:
            if not u:
                return u
            p = urlparse(u)
            if not p.scheme:
                if u.startswith('//'):
                    base_p = urlparse(base_url)
                    scheme = base_p.scheme or 'https'
                    return f"{scheme}:{u}"
                return urljoin(base_url, u)
            # Prefer https if base is https
            base_p = urlparse(base_url)
            if p.scheme == 'http' and (base_p.scheme or '').lower() == 'https':
                return u.replace('http://', 'https://', 1)
            return u

        if not html:
            return None

        # 1) Amazon-specific image extraction
        if 'amazon.' in base_url.lower():
            # Amazon product images - multiple methods
            amazon_patterns = [
                # Main product image (landingImage data)
                r'"colorImages":\s*{\s*"initial":\s*\[\s*{\s*"large":\s*"([^"]+)"',
                r'"landingImage":\s*"([^"]+)"',
                # Image gallery
                r'"hiRes":\s*"([^"]+)"',
                r'"large":\s*"([^"]+)"',
                # Fallback to img tag with specific ID
                r'<img[^>]+id=["\']landingImage["\'][^>]*src=["\']([^"\']+)["\']',
                r'<img[^>]+data-old-hires=["\']([^"\']+)["\']',
                r'<img[^>]+data-a-dynamic-image=["\'][{][^}]*["\']([^"\']+)["\']',
            ]
            for pat in amazon_patterns:
                m = re.search(pat, html, re.I)
                if m:
                    img_url = m.group(1).strip()
                    # Clean up Amazon image URLs (remove size constraints for better quality)
                    img_url = re.sub(r'\._[A-Z0-9,_]+_\.', '.', img_url)
                    return _abs(img_url)

        # 2) JSON-LD Product image
        def _find_image(obj):
            if isinstance(obj, dict):
                for k in ('image', 'imageUrl', 'thumbnailUrl'):
                    if k in obj:
                        v = obj[k]
                        if isinstance(v, str):
                            return v
                        if isinstance(v, list) and v and isinstance(v[0], str):
                            # choose first
                            return v[0]
                for v in obj.values():
                    out = _find_image(v)
                    if out:
                        return out
            elif isinstance(obj, list):
                for it in obj:
                    out = _find_image(it)
                    if out:
                        return out
            return None

        for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
            block = m.group(1).strip()
            try:
                data = _json.loads(block)
            except (_json.JSONDecodeError, ValueError, TypeError):
                continue
            img = _find_image(data)
            if img:
                return _abs(img.strip())

        # 2) OG/Twitter meta
        meta_patterns = (
            r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']twitter:image(?:\:src)?["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']twitter:image(?:\:src)?["\'][^>]*content=["\']([^"\']+)["\']',
        )
        for pat in meta_patterns:
            m = re.search(pat, html, re.I)
            if m:
                return _abs(m.group(1).strip())

        # 3) <img> with product-ish cues, prefer zoom/large attributes
        # 3a) data-zoom-image / data-large_image / data-src
        attr_patterns = (
            r'<img[^>]+data-zoom-image=["\']([^"\']+)["\']',
            r'<img[^>]+data-large[_-]image=["\']([^"\']+)["\']',
            r'<img[^>]+data-src=["\']([^"\']+)["\']',
        )
        for pat in attr_patterns:
            m = re.search(pat, html, re.I)
            if m:
                return _abs(m.group(1).strip())

        # 3b) srcset: choose the largest width
        m = re.search(r'<img[^>]+srcset=["\']([^"\']+)["\'][^>]*', html, re.I)
        if m:
            srcset = m.group(1)
            # parse entries: url [Nw]
            candidates = []
            for part in srcset.split(','):
                seg = part.strip()
                if not seg:
                    continue
                pieces = seg.split()
                url = pieces[0]
                w = 0
                if len(pieces) > 1 and pieces[1].endswith('w'):
                    try:
                        w = int(pieces[1][:-1])
                    except ValueError:
                        w = 0
                candidates.append((w, url))
            if candidates:
                candidates.sort()
                return _abs(candidates[-1][1].strip())

        # 3c) generic product/main image class or alt pattern
        patterns = [
            r'<img[^>]+class=["\'][^"\']*(?:product|main|primary|image)[^"\']*["\'][^>]*src=["\']([^"\']+)["\']',
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*alt=["\'][^"\']*(?:product|frame|brake|derailleur|hoops|dream|machine|u7)["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                return _abs(m.group(1).strip())

        return None

    def _fetch_product_image(url: str) -> str | None:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        cached = _IMAGE_CACHE.get(url)
        if cached and (now - cached['fetched']) < timedelta(hours=24):
            return cached['image_url']
        if not _is_public_url(url):
            return None
        try:
            # Use enhanced headers for Amazon
            headers = {'User-Agent': flask_app.config['USER_AGENT']}
            if 'amazon.' in url.lower():
                headers.update({
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                })
            
            resp = requests.get(url, headers=headers, timeout=flask_app.config['TIMEOUT'])
            if resp.status_code >= 400:
                return None
            img = _extract_image_url(resp.text, url)
            if img and not _is_public_url(img):
                return None
            if img:
                _IMAGE_CACHE[url] = {'image_url': img, 'fetched': now}
            return img
        except requests.exceptions.RequestException:
            return None

    def _ensure_cached_image_file(product_url: str, max_w: int, max_h: int):
        """Ensure a resized, cached image exists for the given product URL.
        Returns Path to cached file or None on failure.
        """
        from io import BytesIO
        import hashlib
        import mimetypes
        from pathlib import Path
        from PIL import Image, ImageOps

        image_url = _fetch_product_image(product_url)
        if not image_url:
            return None
        key = hashlib.sha256(f"{image_url}|{max_w}x{max_h}".encode('utf-8')).hexdigest()
        guessed_ext = (Path(image_url).suffix or '').lower()
        ext = '.png' if guessed_ext in ('.png', '.webp') else '.jpg'
        
        # Use absolute path from HISTORY_DB config to find data directory
        data_dir = Path(flask_app.config.get('HISTORY_DB', 'data/history.db')).parent
        images_dir = data_dir / 'images'
        images_dir.mkdir(parents=True, exist_ok=True)
        out_path = images_dir / f"{key}{ext}"
        if out_path.exists():
            return out_path
        try:
            if not _is_public_url(image_url):
                return None
            r = requests.get(image_url, headers={'User-Agent': flask_app.config['USER_AGENT']}, timeout=flask_app.config['TIMEOUT'], stream=True)
            if r.status_code >= 400:
                return None
            content_type = r.headers.get('Content-Type', '').lower()
            if not content_type.startswith('image/'):
                # Reject non-image content types — don't trust URL extension alone
                return None
            # Enforce a hard cap on downloaded bytes (e.g., 5 MiB)
            max_bytes = int(os.getenv('IMAGE_MAX_BYTES', '5242880'))
            read = 0
            buf = BytesIO()
            for chunk in r.iter_content(65536):
                if not chunk:
                    break
                read += len(chunk)
                if read > max_bytes:
                    return None
                buf.write(chunk)
            buf.seek(0)
            raw = buf
        except requests.exceptions.RequestException:
            return None
        try:
            with Image.open(raw) as im:
                im = ImageOps.exif_transpose(im)
                if im.mode in ('P', 'LA'):
                    im = im.convert('RGBA')
                im.thumbnail((max_w, max_h), Image.LANCZOS)
                has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
                fmt = 'PNG' if has_alpha or ext == '.png' else 'JPEG'
                if fmt == 'JPEG' and im.mode not in ('RGB', 'L'):
                    im = im.convert('RGB')
                save_kwargs = {'quality': 85, 'optimize': True} if fmt == 'JPEG' else {}
                out_tmp = out_path.with_suffix(out_path.suffix + '.tmp')
                with open(out_tmp, 'wb') as f:
                    im.save(f, format=fmt, **save_kwargs)
                out_tmp.replace(out_path)
        except Exception:
            return None
        return out_path

    @flask_app.route('/api/product/image')
    @require_api_key_for_reads
    def api_product_image():
        product_url = request.args.get('url')
        if not product_url:
            return jsonify({'error': 'url parameter required'}), 400
        if not product_url.lower().startswith(('http://', 'https://')):
            return jsonify({'error': 'invalid url'}), 400
        img = _fetch_product_image(product_url)
        if not img:
            return jsonify({'image_url': None, 'cached': False}), 404
        return jsonify({'image_url': img, 'cached': True})

    @flask_app.route('/api/product/image/file')
    @require_api_key_for_reads
    @_rate_limit("30 per minute")
    def api_product_image_file():
        """Proxy, resize, and cache a product image to serve locally.

        Query params:
        - url: product page URL (required)
        - w: max width in px (optional, default 600)
        - h: max height in px (optional, default 220)
        """
        from pathlib import Path

        product_url = request.args.get('url', type=str)
        if not product_url:
            return jsonify({'error': 'url parameter required'}), 400
        if not product_url.lower().startswith(('http://', 'https://')):
            return jsonify({'error': 'invalid url'}), 400
        
        try:
            max_w = int(request.args.get('w', 600))
            max_h = int(request.args.get('h', 220))
        except ValueError:
            max_w, max_h = 600, 220
        max_w = max(1, min(max_w, 4096))
        max_h = max(1, min(max_h, 4096))
        
        try:
            out_path = _ensure_cached_image_file(product_url, max_w, max_h)
            if out_path and Path(out_path).exists():
                resp = send_file(out_path, conditional=True)
                resp.headers['Cache-Control'] = 'public, max-age=86400'
                return resp
            # If caching failed, return 404 instead of 500
            return jsonify({'error': 'image not available'}), 404
        except Exception as e:
            logging.error("Error serving image for %s: %s", product_url, e, exc_info=True)
            return jsonify({'error': 'image processing failed'}), 404

    # ---------------- Periodic image warmup -----------------
    def _warmup_images_once():
        """Prefetch and cache images for enabled products with basic cross-process throttling."""
        try:
            product_store = flask_app.config['PRODUCT_STORE']
            products = product_store.get_all()
        except Exception:
            return
        from pathlib import Path
        
        # Use absolute path from HISTORY_DB config
        data_dir = Path(flask_app.config.get('HISTORY_DB', 'data/history.db')).parent
        images_dir = data_dir / 'images'
        images_dir.mkdir(parents=True, exist_ok=True)
        stamp_path = images_dir / '.last_warmup'
        lock = FileLock(str(images_dir / 'warmup'))
        try:
            lock.acquire()
            from datetime import timedelta
            now = datetime.now(timezone.utc)
            last = None
            try:
                if stamp_path.exists():
                    ts = stamp_path.read_text().strip()
                    last = datetime.fromisoformat(ts)
            except Exception:
                last = None
            interval_min = int(os.getenv('IMAGE_WARMUP_INTERVAL_MIN', '360'))
            if last and (now - last) < timedelta(minutes=interval_min):
                return
            max_w = int(os.getenv('IMAGE_CACHE_WIDTH', '600'))
            max_h = int(os.getenv('IMAGE_CACHE_HEIGHT', '220'))
            for p in products:
                try:
                    if not getattr(p, 'enabled', True):
                        continue
                    url = getattr(p, 'url', None)
                    if not url or not str(url).lower().startswith(('http://','https://')):
                        continue
                    _ensure_cached_image_file(url, max_w, max_h)
                except Exception:
                    continue
            try:
                stamp_path.write_text(datetime.now(timezone.utc).isoformat())
            except Exception:
                pass
        finally:
            try:
                lock.release()
            except Exception:
                pass

    def _warmup_loop():
        import time
        try:
            interval_min = int(os.getenv('IMAGE_WARMUP_INTERVAL_MIN', '360'))
        except ValueError:
            interval_min = 360
        time.sleep(5)
        _warmup_images_once()
        while True:
            time.sleep(max(60, interval_min * 60))
            _warmup_images_once()

    if os.getenv('ENABLE_IMAGE_WARMUP', '1').strip().lower() not in ('0','false','no'):
        import threading
        t = threading.Thread(target=_warmup_loop, name='image-warmup', daemon=True)
        t.start()
    
    return flask_app


if __name__ == '__main__':
    app = create_app()
    # Listen on all interfaces in production (Docker), localhost only in dev
    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    app.run(host=host, port=5000, debug=(os.getenv('FLASK_ENV') != 'production'))