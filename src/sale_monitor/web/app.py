"""
Flask app factory for the Sale Monitor dashboard.

Routes live in ``web/routes/`` blueprints; shared objects (stores, caches,
services) are created here and stashed in ``app.config``.
"""
import logging
import os
import time

from flask import Flask, jsonify, request

from sale_monitor.services.exchange_rates import ExchangeRateService
from sale_monitor.services.price_check import PriceCheckService
from sale_monitor.services.product_images import ImageService
from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.storage.price_history import PriceHistory
from sale_monitor.storage.product_store import ProductStore
from sale_monitor.storage.purchase_store import PurchaseStore
from sale_monitor.web import extensions
from sale_monitor.web.helpers import CachedProductStore, CachedState

logger = logging.getLogger(__name__)


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

    # Initialize product store (SQLite-backed) with read cache
    _raw_product_store = ProductStore(flask_app.config['HISTORY_DB'])
    product_store = CachedProductStore(_raw_product_store)
    flask_app.config['PRODUCT_STORE'] = product_store

    # Auto-import from CSV on first run (one-time migration)
    csv_path = flask_app.config['PRODUCTS_CSV']
    if os.path.exists(csv_path):
        imported = product_store.auto_import_csv(csv_path)
        if imported:
            logger.info("Imported %d products from %s into database", imported, csv_path)

    # Shared mtime-cached state reader (avoids re-parsing JSON on every request)
    flask_app.config['_STATE_CACHE'] = CachedState(flask_app.config['STATE_FILE'])

    # Shared services
    _shared_history = PriceHistory(flask_app.config['HISTORY_DB'])
    _shared_ex_service = ExchangeRateService(cache_handler=_shared_history)
    flask_app.config['_HISTORY'] = _shared_history
    flask_app.config['_EX_SERVICE'] = _shared_ex_service
    flask_app.config['_PURCHASE_STORE'] = PurchaseStore(flask_app.config['HISTORY_DB'])
    flask_app.config['_CHECK_SERVICE'] = PriceCheckService(
        user_agent=flask_app.config['USER_AGENT'],
        timeout=flask_app.config['TIMEOUT'],
        max_retries=flask_app.config['MAX_RETRIES'],
        history=_shared_history,
        ex_service=_shared_ex_service,
        config_file=flask_app.config['CONFIG_FILE'],
    )
    flask_app.config['_IMAGE_SERVICE'] = ImageService(
        user_agent=flask_app.config['USER_AGENT'],
        timeout=flask_app.config['TIMEOUT'],
        data_dir=str(os.path.dirname(flask_app.config['HISTORY_DB']) or 'data'),
    )

    # Per-app caches / bookkeeping
    flask_app.config['_ALERTS_CACHE'] = {'mtime': None, 'data': []}
    flask_app.config['_APP_START_TIME'] = time.time()

    # Rate limiting
    extensions.init_app(flask_app)

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

    # --------------- Blueprints ---------------
    from sale_monitor.web.routes import (
        alerts,
        compare,
        health,
        history,
        images,
        pages,
        products,
        purchases,
        settings,
    )
    for module in (pages, products, history, alerts, compare, settings, health, images, purchases):
        flask_app.register_blueprint(module.bp)

    return flask_app


if __name__ == '__main__':
    app = create_app()
    # Listen on all interfaces in production (Docker), localhost only in dev
    host = '0.0.0.0' if os.getenv('FLASK_ENV') == 'production' else '127.0.0.1'
    app.run(host=host, port=5000, debug=(os.getenv('FLASK_ENV') != 'production'))
