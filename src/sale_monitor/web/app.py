"""
Flask web application for Sale Monitor dashboard.
"""
from flask import Flask, render_template, jsonify, request
from datetime import datetime
import os

from sale_monitor.storage.csv_products import read_products
from sale_monitor.storage.json_state import load_state
from sale_monitor.storage.price_history import PriceHistory


def create_app():
    """Create and configure Flask application."""
    app = Flask(__name__)
    
    # Configuration
    app.config['PRODUCTS_CSV'] = os.getenv('PRODUCTS_CSV', 'data/products.csv')
    app.config['STATE_FILE'] = os.getenv('STATE_FILE', 'data/state.json')
    app.config['HISTORY_DB'] = os.getenv('HISTORY_DB', 'data/history.db')
    
    @app.route('/')
    def index():
        """Dashboard home page."""
        return render_template('index.html')
    
    @app.route('/product/detail')
    def product_detail():
        """Product detail page with history chart."""
        return render_template('product_detail.html')
    
    @app.route('/api/products')
    def api_products():
        """Get all products with current state."""
        try:
            products = read_products(app.config['PRODUCTS_CSV'])
            state = load_state(app.config['STATE_FILE'])
            
            result = []
            for p in products:
                state_data = state.get(p.url, {})
                result.append({
                    'name': p.name,
                    'url': p.url,
                    'current_price': state_data.get('current_price'),
                    'target_price': p.target_price,
                    'discount_threshold': p.discount_threshold,
                    'last_checked': state_data.get('last_checked'),
                    'last_price': state_data.get('last_price'),
                    'enabled': p.enabled,
                    'selector': p.selector,
                })
            
            return jsonify(result)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/product/history')
    def api_product_history():
        """Get price history for a product."""
        try:
            url = request.args.get('url')
            if not url:
                return jsonify({'error': 'URL parameter required'}), 400
            
            history = PriceHistory(app.config['HISTORY_DB'])
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
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/product/stats')
    def api_product_stats():
        """Get statistics for a product."""
        try:
            url = request.args.get('url')
            if not url:
                return jsonify({'error': 'URL parameter required'}), 400
            
            history = PriceHistory(app.config['HISTORY_DB'])
            days = int(request.args.get('days', 30))
            
            stats = history.get_stats(url, days=days)
            
            return jsonify(stats)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    
    return app


if __name__ == '__main__':
    app = create_app()
    app.run(host='127.0.0.1', port=5000, debug=True)