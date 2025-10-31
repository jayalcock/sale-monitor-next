"""
Flask web application for Sale Monitor dashboard.
"""
from flask import Flask, render_template, jsonify, request, Response
from datetime import datetime
import os
import csv
import sqlite3
import requests

from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state, save_state
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.domain.models import Product
from sale_monitor.storage.file_lock import FileLock


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
            
            result = []
            for p in products:
                state_data = state.get(p.url, {})
                result.append({
                    'name': p.name,
                    'url': p.url,
                    'current_price': state_data.get('current_price'),
                    'target_price': p.target_price,
                    'discount_threshold': p.discount_threshold,
                    'notification_cooldown_hours': p.notification_cooldown_hours,
                    'last_checked': state_data.get('last_checked'),
                    'last_price': state_data.get('last_price'),
                    'enabled': p.enabled,
                    'selector': p.selector,
                })
            
            return jsonify(result)
        except (OSError, ValueError) as e:
            return jsonify({'error': str(e)}), 500
    
    @flask_app.route('/api/product/history')
    def api_product_history():
        """Get price history for a product."""
        try:
            url = request.args.get('url')
            if not url:
                return jsonify({'error': 'URL parameter required'}), 400
            
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            days = int(request.args.get('days', 30))
            
            records = history.get_history(url, days=days)
            
            result = [
                {
                    'timestamp': timestamp,
                    'price': price,
                    'status': status
                }
                for timestamp, price, status in records
            ]
            
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
            price = extractor.extract_price(product.url, product.selector)
            
            if price is None:
                return jsonify({'error': 'Failed to extract price'}), 500
            
            # Update state
            state = load_state(flask_app.config['STATE_FILE'])
            state[url] = {
                'current_price': price,
                'last_checked': datetime.now().isoformat(),
                'last_price': state.get(url, {}).get('current_price', price)
            }
            save_state(flask_app.config['STATE_FILE'], state)
            
            # Record in history (count as success so stats include manual checks)
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            history.record_price(product.url, product.name, price, status='success')
            
            return jsonify({
                'success': True,
                'price': price,
                'timestamp': state[url]['last_checked']
            })
        except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
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
    
    @flask_app.route('/api/product/add', methods=['POST'])
    def api_add_product():
        """Add a new product."""
        try:
            data = request.get_json()
            
            # Validate required fields
            required = ['name', 'url', 'selector']
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
                selector=data['selector'],
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
            
            history = PriceHistory(flask_app.config['HISTORY_DB'])
            
            # Stream CSV directly to avoid temp files
            output = StringIO()
            history.export_to_csv_stream(output)
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
    
    return flask_app


def _write_products_csv(filepath, products):
    """Helper to write products to CSV file."""
    lock = FileLock(filepath)
    lock.acquire()
    try:
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['name', 'url', 'target_price', 'discount_threshold', 'selector', 'enabled', 'notification_cooldown_hours'])
            for p in products:
                writer.writerow([
                    p.name,
                    p.url,
                    p.target_price if p.target_price is not None else '',
                    p.discount_threshold if p.discount_threshold is not None else '',
                    p.selector,
                    'true' if p.enabled else 'false',
                    p.notification_cooldown_hours
                ])
    finally:
        lock.release()



if __name__ == '__main__':
    app = create_app()
    # Listen on all interfaces in production (Docker), localhost only in dev
    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    app.run(host=host, port=5000, debug=(os.getenv('FLASK_ENV') != 'production'))