"""
Flask web application for Sale Monitor dashboard.
"""
from flask import Flask, render_template, jsonify, request, Response
from flask import send_file
from datetime import datetime, timezone
import os
import csv
import sqlite3
import requests
import logging

from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state, save_state
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.services.exchange_rates import ExchangeRateService
from sale_monitor.domain.models import Product
from sale_monitor.storage.file_lock import FileLock
from sale_monitor.storage.config_store import load_config, save_config, get_base_currency


def create_app():
    """Create and configure Flask application."""
    flask_app = Flask(__name__)
    
    # Configuration
    flask_app.config['PRODUCTS_CSV'] = os.getenv('PRODUCTS_CSV', 'data/products.csv')
    flask_app.config['STATE_FILE'] = os.getenv('STATE_FILE', 'data/state.json')
    flask_app.config['HISTORY_DB'] = os.getenv('HISTORY_DB', 'data/history.db')
    flask_app.config['USER_AGENT'] = os.getenv('USER_AGENT', 'Mozilla/5.0 (compatible; SaleMonitor/1.0)')
    flask_app.config['TIMEOUT'] = int(os.getenv('TIMEOUT', '30'))
    flask_app.config['MAX_RETRIES'] = int(os.getenv('MAX_RETRIES', '3'))
    flask_app.config['CONFIG_FILE'] = os.getenv('CONFIG_FILE', 'data/config.json')
    # Initial/base currency from env or config file
    initial_base_currency = os.getenv('BASE_CURRENCY') or get_base_currency(flask_app.config['CONFIG_FILE'])
    flask_app.config['BASE_CURRENCY'] = initial_base_currency.upper()
    
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
    
    @flask_app.route('/api/products')
    def api_products():
        """Get all products with current state."""
        try:
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            state = load_state(flask_app.config['STATE_FILE'])
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE'])

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
                            history = PriceHistory(flask_app.config['HISTORY_DB'])
                            ex_service = ExchangeRateService(cache_handler=history)
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
                })

            return jsonify(result)
        except (OSError, ValueError, sqlite3.Error) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/stats')
    def api_product_stats():
        """Get statistics for a product."""
        try:
            url = request.args.get('url')
            if not url:
                return jsonify({'error': 'URL parameter required'}), 400

            history = PriceHistory(flask_app.config['HISTORY_DB'])
            days = int(request.args.get('days', 30))

            stats = history.get_stats(url, days=days)
            return jsonify(stats)
        except (OSError, ValueError, sqlite3.Error) as e:
            return jsonify({'error': str(e)}), 500

    @flask_app.route('/api/product/history')
    def api_product_history():
        """Get price history for a single product.

        Response format: list of objects [{timestamp, price, currency, price_in_base}] newest-first.
        price_in_base is always expressed in current configured base currency.
        """
        try:
            url = request.args.get('url')
            if not url:
                return jsonify({'error': 'URL parameter required'}), 400

            days = int(request.args.get('days', 30))
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            ex_service = ExchangeRateService(cache_handler=history)
            base_currency = get_base_currency(flask_app.config['CONFIG_FILE']).upper()

            records = history.get_history_extended(url, days=days)

            # If no DB records, synthesize one from state so chart isn't blank
            if not records:
                st = load_state(flask_app.config['STATE_FILE']).get(url)
                if st and 'current_price' in st:
                    cur = st.get('current_price')
                    cur_currency = (st.get('currency') or 'CAD').upper()
                    records = [
                        (st.get('last_checked') or datetime.now(timezone.utc).isoformat(), cur, 'success', cur_currency, None)
                    ]

            result = []
            for (ts, price, status, currency, _) in records:
                if status != 'success':
                    continue

                # Compute price in configured base currency
                price_in_base = None
                try:
                    if price is not None and currency:
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

            return jsonify(result)
        except (OSError, ValueError, sqlite3.Error) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/toggle', methods=['POST'])
    def api_toggle_product():
        """Toggle product enabled status."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            # Read all products
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            
            # Find and toggle the product
            found = False
            updated_products = []
            updated_products = []
            for p in products:
                if p.url == url:
                    p.enabled = not p.enabled
                    found = True
                updated_products.append(p)
            
            if not found:
                return jsonify({'error': 'Product not found'}), 404
            
            # Write back to CSV
            _write_products_csv(flask_app.config['PRODUCTS_CSV'], updated_products)
            
            return jsonify({'success': True, 'enabled': [p for p in updated_products if p.url == url][0].enabled})
        except (OSError, ValueError) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/check', methods=['POST'])
    def api_check_product():
        """Manually trigger price check for a product."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            # Find product
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            product = next((p for p in products if p.url == url), None)
            
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
            state[url] = {
                'current_price': price,
                'last_checked': datetime.now(timezone.utc).isoformat(),
                'last_price': state.get(url, {}).get('current_price', price),
                'selector_source': selector_source,
                'currency': currency,
                'currency_source': currency_source,
                'price_in_base': price_in_base
            }
            save_state(flask_app.config['STATE_FILE'], state)
            
            # Record in history (count as success so stats include manual checks)
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            history.record_price(product.url, product.name, price, status='success', currency=currency)
            
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
            return jsonify({'error': str(e)}), 500

    @flask_app.route('/api/products/check-all', methods=['POST'])
    def api_check_all_products():
        """Trigger price check for all enabled products.

        Returns summary of successes / failures.
        """
        try:
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            enabled = [p for p in products if p.enabled]
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
                        failed += 1
                        continue
                    price, selector_source, detected_currency = extractor.extract_price_with_currency(product_url, p.selector, default_currency=p.currency)
                    if price is None:
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
                        'price_in_base': price_in_base
                    }
                    history.record_price(p.url, p.name, price, status='success', currency=currency)
                    updated += 1
                except (requests.exceptions.RequestException, ValueError, sqlite3.Error):
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
            return jsonify({'error': str(e)}), 500

    @flask_app.route('/api/config', methods=['GET'])
    def api_get_config():
        cfg = load_config(flask_app.config['CONFIG_FILE'])
        return jsonify({'base_currency': cfg.get('base_currency', 'CAD')})

    @flask_app.route('/api/config/base-currency', methods=['POST'])
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
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/delete', methods=['POST'])
    def api_delete_product():
        """Delete a product."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            # Read and filter products
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            filtered = [p for p in products if p.url != url]
            
            if len(filtered) == len(products):
                return jsonify({'error': 'Product not found'}), 404
            
            # Write back
            _write_products_csv(flask_app.config['PRODUCTS_CSV'], filtered)
            
            return jsonify({'success': True})
        except (OSError, ValueError) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/products/auto-detect-all', methods=['POST'])
    def api_auto_detect_all():
        """Attempt to auto-detect price selectors for all products."""
        try:
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            
            extractor = PriceExtractor(
                user_agent=flask_app.config['USER_AGENT'],
                timeout=flask_app.config['TIMEOUT'],
                max_retries=flask_app.config['MAX_RETRIES']
            )
            
            successful = 0
            failed = 0
            updated_products = []
            
            for product in products:
                try:
                    # Try to extract price with empty selector to force auto-detection
                    price, selector_source = extractor.extract_price(product.url, "")
                    
                    if price is not None and selector_source == 'auto':
                        # Auto-detection succeeded - clear selector and mark as auto
                        product.selector = ''
                        product.selector_source = 'auto'
                        successful += 1
                    else:
                        # Keep existing product unchanged
                        failed += 1
                    
                    updated_products.append(product)
                except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
                    logging.error("Auto-detect failed for %s: %s", product.url, e)
                    updated_products.append(product)  # Keep original
                    failed += 1
            
            # Write updated products back to CSV
            _write_products_csv(flask_app.config['PRODUCTS_CSV'], updated_products)
            
            return jsonify({
                'success': True,
                'successful': successful,
                'failed': failed
            })
        except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
            logging.error("Bulk auto-detect error: %s", e)
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/add', methods=['POST'])
    def api_add_product():
        """Add a new product."""
        try:
            data = request.get_json()
            
            # Validate required fields (selector is now optional)
            required = ['name', 'url']
            for field in required:
                if not data.get(field):
                    return jsonify({'error': f'{field} is required'}), 400
            
            # Parse and validate optional numeric fields
            def _parse_float(val, field_name):
                if val in (None, ''):
                    return None
                try:
                    return float(val)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f'{field_name} must be a valid number') from exc

            def _parse_int(val, field_name, default=None):
                if val in (None, ''):
                    return default
                try:
                    parsed = int(val)
                    if parsed < 0:
                        raise ValueError(f'{field_name} must be a positive number')
                    return parsed
                except (TypeError, ValueError) as e:
                    raise ValueError(f'{field_name} must be a valid positive integer') from e

            try:
                target_price = _parse_float(data.get('target_price'), 'target_price')
                discount_threshold = _parse_float(data.get('discount_threshold'), 'discount_threshold')
                cooldown_hours = _parse_int(data.get('notification_cooldown_hours'), 'notification_cooldown_hours', default=24)
            except ValueError as ve:
                return jsonify({'error': str(ve)}), 400

            # Create product
            new_product = Product(
                name=data['name'],
                url=data['url'],
                target_price=target_price,
                discount_threshold=discount_threshold,
                selector=data.get('selector', ''),  # Default to empty string if not provided
                enabled=data.get('enabled', True),
                notification_cooldown_hours=cooldown_hours
            )
            
            # Read existing products
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            
            # Check for duplicate URL
            if any(p.url == new_product.url for p in products):
                return jsonify({'error': 'Product with this URL already exists'}), 400
            
            # Add and save
            products.append(new_product)
            _write_products_csv(flask_app.config['PRODUCTS_CSV'], products)
            
            return jsonify({'success': True, 'product': {
                'name': new_product.name,
                'url': new_product.url,
                'enabled': new_product.enabled,
                'notification_cooldown_hours': new_product.notification_cooldown_hours
            }})
        except (OSError, ValueError) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/update', methods=['POST'])
    def api_update_product():
        """Update an existing product."""
        try:
            data = request.get_json()
            url = data.get('url')
            if not url:
                return jsonify({'error': 'URL required'}), 400
            
            # Read products
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            
            # Find and update
            found = False
            for i, p in enumerate(products):
                if p.url == url:
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
                            if parsed < 0:
                                raise ValueError(f'{field_name} must be a positive number')
                            return parsed
                        except (TypeError, ValueError) as exc:
                            raise ValueError(f'{field_name} must be a valid positive integer') from exc

                    try:
                        target_price = _parse_float(data.get('target_price'), p.target_price, 'target_price')
                        discount_threshold = _parse_float(data.get('discount_threshold'), p.discount_threshold, 'discount_threshold')
                        cooldown_hours = _parse_int(data.get('notification_cooldown_hours'), p.notification_cooldown_hours, 'notification_cooldown_hours')
                    except ValueError as ve:
                        return jsonify({'error': str(ve)}), 400

                    products[i] = Product(
                        name=data.get('name', p.name),
                        url=url,
                        target_price=target_price,
                        discount_threshold=discount_threshold,
                        selector=data.get('selector', p.selector),
                        enabled=data.get('enabled', p.enabled),
                        notification_cooldown_hours=cooldown_hours
                    )
                    found = True
                    break
            
            if not found:
                return jsonify({'error': 'Product not found'}), 404
            
            _write_products_csv(flask_app.config['PRODUCTS_CSV'], products)
            
            updated = next((pp for pp in products if pp.url == url), None)
            return jsonify({'success': True, 'product': {
                'name': updated.name,
                'url': updated.url,
                'enabled': updated.enabled,
                'notification_cooldown_hours': updated.notification_cooldown_hours,
                'target_price': updated.target_price,
                'discount_threshold': updated.discount_threshold,
                'selector': updated.selector
            }})
        except (OSError, ValueError) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/alerts')
    def api_alerts():
        """Get products that have hit their price targets or discount thresholds."""
        try:
            products = read_products(flask_app.config['PRODUCTS_CSV'])
            state = load_state(flask_app.config['STATE_FILE'])

            alerts = []
            for p in products:
                if not p.enabled:
                    continue

                state_data = state.get(p.url, {})
                current = state_data.get('current_price')
                last = state_data.get('last_price')

                if current is None:
                    continue

                alert_type = None
                message = None

                # Check target price
                if p.target_price and current <= p.target_price:
                    alert_type = 'target_met'
                    message = f'Price ${current:.2f} is at or below target ${p.target_price:.2f}'

                # Check discount threshold
                elif p.discount_threshold and last:
                    discount = ((last - current) / last) * 100
                    if discount >= p.discount_threshold:
                        alert_type = 'discount_met'
                        message = f'Price dropped {discount:.1f}% (${last:.2f} → ${current:.2f})'

                if alert_type:
                    alerts.append({
                        'name': p.name,
                        'url': p.url,
                        'current_price': current,
                        'alert_type': alert_type,
                        'message': message,
                        'last_checked': state_data.get('last_checked')
                    })

            return jsonify(alerts)
        except (OSError, ValueError) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/export/history')
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
            return jsonify({'error': str(e)}), 500

    @flask_app.route('/api/history/all')
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
            # Prefer names from current products.csv to avoid stale/incorrect names in DB
            try:
                current_products = read_products(flask_app.config['PRODUCTS_CSV'])
                name_by_url = {p.url: p.name for p in current_products}
            except (OSError, ValueError):
                name_by_url = {}

            # Get list of products that have any history
            db_products = history.get_all_products()  # List[Tuple[url, name]]
            
            # If DB is empty but we have CSV products, use those to enable fallback synthesis
            if not db_products and name_by_url:
                products = [(url, name) for url, name in name_by_url.items()]
            else:
                products = db_products
            
            result = []
            seen_urls = set()

            for url, name in products:
                # If we have a current CSV mapping, restrict to those URLs only
                if name_by_url and url not in name_by_url:
                    continue
                # Deduplicate by URL in case DB has multiple names over time
                if url in seen_urls:
                    continue
                records = history.get_history_extended(url, days=days)
                if not records:
                    # Fallback: synthesize a single point from current state so trends don't go blank
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
                # Choose display name: prefer CSV; else DB unless it looks numeric -> fallback to URL
                display_name = name_by_url.get(url, name)
                try:
                    # If display_name is numeric-like (legacy bug), fallback to CSV or URL
                    if display_name is not None and str(display_name).strip() != "":
                        _ = float(str(display_name))
                        # numeric, so replace
                        display_name = name_by_url.get(url, url)
                except ValueError:
                    pass
                series = []
                for (ts, price, status, currency, _) in records:
                    if status != 'success':
                        continue

                    # Compute price in configured base currency
                    price_in_base = None
                    try:
                        if price is not None and currency:
                            if currency.upper() == base_currency:
                                price_in_base = price
                            else:
                                converted_base = ex_service.convert(float(price), currency.upper(), base_currency)
                                price_in_base = converted_base if converted_base is not None else None
                    except (ValueError, TypeError):
                        pass

                    # Round to 2 decimals
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
                seen_urls.add(url)

            return jsonify(result)
        except (OSError, ValueError, sqlite3.Error) as e:
            return jsonify({'error': str(e)}), 500

    @flask_app.route('/api/health')
    def api_health():
        """Basic health check: DB integrity and history row count."""
        try:
            with sqlite3.connect(flask_app.config['HISTORY_DB']) as conn:
                try:
                    ok_row = conn.execute("PRAGMA integrity_check").fetchone()
                    ok = bool(ok_row and ok_row[0] == 'ok')
                except sqlite3.Error:
                    ok = False
                try:
                    row = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()
                    rows = int(row[0]) if row else 0
                except sqlite3.Error:
                    rows = 0
            return jsonify({'integrity_ok': ok, 'rows': rows})
        except (sqlite3.Error, OSError, ValueError) as e:
            return jsonify({'integrity_ok': False, 'error': str(e)}), 500

    # ---------------- Product Image Endpoint (added) -----------------
    # Simple in-memory image cache: {url: {image_url: str, fetched: datetime}}
    _IMAGE_CACHE = {}

    def _extract_image_url(html: str, base_url: str) -> str | None:
        """Extract representative product image URL from HTML.
        Prefers og:image; falls back to heuristic img tag patterns."""
        import re
        from urllib.parse import urlparse, urljoin
        if not html:
            return None
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]+name=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<img[^>]+class=["\'][^"\']*(?:product|main|primary|image)[^"\']*["\'][^>]*src=["\']([^"\']+)["\']',
            r'<img[^>]+src=["\']([^"\']+)["\'][^>]*alt=["\'][^"\']*(?:product|frame|brake|derailleur|hoops|dream|machine|u7)[^"\']*["\']'
        ]
        candidate = None
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                break
        if not candidate:
            return None
        # urlparse already imported above
        parsed = urlparse(candidate)
        if not parsed.scheme:
            if candidate.startswith('//'):
                base_parsed = urlparse(base_url)
                scheme = base_parsed.scheme or 'https'
                candidate = f"{scheme}:{candidate}"
            else:
                # urljoin already imported above
                candidate = urljoin(base_url, candidate)
        else:
            # If the image is HTTP but base page is HTTPS, try upgrading to HTTPS
            base_parsed = urlparse(base_url)
            if parsed.scheme == 'http' and (base_parsed.scheme or '').lower() == 'https':
                candidate = candidate.replace('http://', 'https://', 1)
        return candidate

    def _fetch_product_image(url: str) -> str | None:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        cached = _IMAGE_CACHE.get(url)
        if cached and (now - cached['fetched']) < timedelta(hours=24):
            return cached['image_url']
        try:
            resp = requests.get(url, headers={'User-Agent': flask_app.config['USER_AGENT']}, timeout=flask_app.config['TIMEOUT'])
            if resp.status_code >= 400:
                return None
            img = _extract_image_url(resp.text, url)
            if img:
                _IMAGE_CACHE[url] = {'image_url': img, 'fetched': now}
            return img
        except requests.exceptions.RequestException:
            return None

    @flask_app.route('/api/product/image')
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
    def api_product_image_file():
        """Proxy, resize, and cache a product image to serve locally.

        Query params:
        - url: product page URL (required)
        - w: max width in px (optional, default 600)
        - h: max height in px (optional, default 220)
        """
        from io import BytesIO
        import hashlib
        import mimetypes
        from pathlib import Path
        from PIL import Image, ImageOps

        product_url = request.args.get('url', type=str)
        if not product_url:
            return jsonify({'error': 'url parameter required'}), 400
        if not product_url.lower().startswith(('http://', 'https://')):
            return jsonify({'error': 'invalid url'}), 400

        # Bounds
        try:
            max_w = int(request.args.get('w', 600))
            max_h = int(request.args.get('h', 220))
        except ValueError:
            max_w, max_h = 600, 220
        max_w = max(1, min(max_w, 4096))
        max_h = max(1, min(max_h, 4096))

        # Resolve the actual image URL (cached for 24h)
        image_url = _fetch_product_image(product_url)
        if not image_url:
            return jsonify({'error': 'image not found'}), 404

        # Disk cache path keyed by image URL + size
        key = hashlib.sha256(f"{image_url}|{max_w}x{max_h}".encode('utf-8')).hexdigest()
        # Choose extension based on original URL hint; fallback to jpg
        guessed_ext = (Path(image_url).suffix or '').lower()
        ext = '.png' if guessed_ext in ('.png', '.webp') else '.jpg'
        images_dir = Path('data') / 'images'
        images_dir.mkdir(parents=True, exist_ok=True)
        out_path = images_dir / f"{key}{ext}"

        # Serve from cache if exists
        if out_path.exists():
            resp = send_file(out_path, conditional=True)
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp

        # Download original image
        try:
            r = requests.get(image_url, headers={'User-Agent': flask_app.config['USER_AGENT']}, timeout=flask_app.config['TIMEOUT'], stream=True)
            if r.status_code >= 400:
                return jsonify({'error': 'failed to fetch source image'}), 502
            content_type = r.headers.get('Content-Type', '').lower()
            if not content_type.startswith('image/'):
                # Try to guess from URL if server doesn't provide an image content-type
                guessed, _ = mimetypes.guess_type(image_url)
                if not (guessed and guessed.startswith('image/')):
                    return jsonify({'error': 'not an image'}), 415
            raw = BytesIO(r.content)
        except requests.exceptions.RequestException:
            return jsonify({'error': 'image fetch error'}), 502

        # Process & resize
        try:
            with Image.open(raw) as im:
                # Handle EXIF orientation and convert as needed
                im = ImageOps.exif_transpose(im)
                # Convert palette/LA to RGBA/RGB as needed
                if im.mode in ('P', 'LA'):
                    im = im.convert('RGBA')
                # Resize preserving aspect ratio
                im.thumbnail((max_w, max_h), Image.LANCZOS)
                # Decide format by alpha presence
                has_alpha = im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info)
                fmt = 'PNG' if has_alpha or ext == '.png' else 'JPEG'
                if fmt == 'JPEG' and im.mode not in ('RGB', 'L'):
                    im = im.convert('RGB')
                # Save to disk
                save_kwargs = {'quality': 85, 'optimize': True} if fmt == 'JPEG' else {}
                out_tmp = out_path.with_suffix(out_path.suffix + '.tmp')
                out_tmp.write_bytes(b'')  # ensure file created for atomic move after save
                with open(out_tmp, 'wb') as f:
                    im.save(f, format=fmt, **save_kwargs)
                out_tmp.replace(out_path)
        except Exception:
            # If processing fails, fall back to streaming original
            try:
                return send_file(BytesIO(r.content), mimetype=content_type or 'application/octet-stream')
            except Exception:
                return jsonify({'error': 'image processing error'}), 500

        resp = send_file(out_path, conditional=True)
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    
    return flask_app


def _write_products_csv(filepath, products):
    """Helper to write products to CSV file."""
    lock = FileLock(filepath)
    lock.acquire()
    try:
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['name', 'url', 'target_price', 'discount_threshold', 'selector', 'enabled', 'notification_cooldown_hours', 'selector_source', 'currency'])
            for p in products:
                writer.writerow([
                    p.name,
                    p.url,
                    p.target_price if p.target_price is not None else '',
                    p.discount_threshold if p.discount_threshold is not None else '',
                    p.selector,
                    'true' if p.enabled else 'false',
                    p.notification_cooldown_hours,
                    p.selector_source if p.selector_source else '',
                    p.currency if hasattr(p, 'currency') else 'CAD'
                ])
    finally:
        lock.release()



if __name__ == '__main__':
    app = create_app()
    # Listen on all interfaces in production (Docker), localhost only in dev
    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    app.run(host=host, port=5000, debug=(os.getenv('FLASK_ENV') != 'production'))