"""Purchase tracking and savings endpoints."""
import sqlite3

from flask import Blueprint, current_app, jsonify, request

from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.web.auth import require_api_key, require_api_key_for_reads
from sale_monitor.web.helpers import (
    config_file,
    get_ex_service,
    get_history,
    get_product_store,
    safe_error,
)

bp = Blueprint('purchases', __name__)


@bp.route('/api/product/purchase', methods=['POST'])
@require_api_key
def api_record_purchase():
    """Record a product purchase and calculate savings."""
    try:
        data = request.get_json()
        url = (data or {}).get('url', '').strip()
        purchase_price = data.get('purchase_price')
        notes = data.get('notes', '')

        if not url or purchase_price is None:
            return jsonify({'error': 'url and purchase_price are required'}), 400

        purchase_price = float(purchase_price)
        product = get_product_store().get_by_url(url)
        if not product:
            return jsonify({'error': 'Product not found'}), 404

        currency = product.currency or 'CAD'
        base_currency = get_base_currency(config_file())
        history = get_history()
        ex_service = get_ex_service()

        # Reference price: max price ever seen (in base currency)
        stats = history.get_stats(url)
        max_price_native = stats.get('max_price') if stats else None

        # Convert purchase price to base currency
        if currency == base_currency:
            purchase_price_base = purchase_price
        else:
            converted = ex_service.convert(purchase_price, currency, base_currency)
            purchase_price_base = round(converted, 2) if converted is not None else None

        # Get max price in base currency from price_history
        reference_price_base = None
        if max_price_native is not None:
            if currency == base_currency:
                reference_price_base = max_price_native
            else:
                # Use max of price_cad column for accuracy
                with sqlite3.connect(current_app.config['HISTORY_DB']) as conn:
                    row = conn.execute(
                        "SELECT MAX(price_cad) FROM price_history "
                        "WHERE product_url = ? AND check_status = 'success' AND price_cad IS NOT NULL",
                        (url,)
                    ).fetchone()
                    reference_price_base = row[0] if row and row[0] else None

        purchase_store = current_app.config['_PURCHASE_STORE']
        result = purchase_store.record_purchase(
            product_url=url,
            product_name=product.name,
            purchase_price=purchase_price,
            currency=currency,
            purchase_price_base=purchase_price_base,
            reference_price=max_price_native,
            reference_price_base=reference_price_base,
            notes=notes,
        )

        return jsonify({
            'success': True,
            'purchase_id': result['id'],
            'savings_base': result['savings_base'],
            'base_currency': base_currency,
            'reference_price': max_price_native,
        })
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    except (OSError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/purchases')
@require_api_key_for_reads
def api_get_purchases():
    """Get purchase history, optionally filtered by product URL."""
    url = request.args.get('url', '').strip() or None
    purchase_store = current_app.config['_PURCHASE_STORE']
    purchases = purchase_store.get_purchases(product_url=url)
    summary = purchase_store.get_total_savings()
    base_currency = get_base_currency(config_file())
    return jsonify({
        'purchases': purchases,
        'total_savings': summary['total_savings'],
        'purchase_count': summary['purchase_count'],
        'base_currency': base_currency,
    })


@bp.route('/api/purchase/delete', methods=['POST'])
@require_api_key
def api_delete_purchase():
    """Delete a purchase record."""
    try:
        data = request.get_json()
        purchase_id = (data or {}).get('id')
        if not purchase_id:
            return jsonify({'error': 'id is required'}), 400
        purchase_store = current_app.config['_PURCHASE_STORE']
        if purchase_store.delete_purchase(int(purchase_id)):
            return jsonify({'success': True})
        return jsonify({'error': 'Purchase not found'}), 404
    except (OSError, sqlite3.Error) as e:
        return safe_error(e)
