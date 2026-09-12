"""Product image endpoints (discover, proxy, resize, cache)."""
import logging
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from sale_monitor.web.auth import require_api_key_for_reads
from sale_monitor.web.extensions import rate_limit
from sale_monitor.web.helpers import get_image_service

logger = logging.getLogger(__name__)

bp = Blueprint('images', __name__)


@bp.route('/api/product/image')
@require_api_key_for_reads
def api_product_image():
    product_url = request.args.get('url')
    if not product_url:
        return jsonify({'error': 'url parameter required'}), 400
    if not product_url.lower().startswith(('http://', 'https://')):
        return jsonify({'error': 'invalid url'}), 400
    img = get_image_service().fetch_image_url(product_url)
    if not img:
        return jsonify({'image_url': None, 'cached': False}), 404
    return jsonify({'image_url': img, 'cached': True})


@bp.route('/api/product/image/file')
@require_api_key_for_reads
@rate_limit("30 per minute")
def api_product_image_file():
    """Proxy, resize, and cache a product image to serve locally.

    Query params:
    - url: product page URL (required)
    - w: max width in px (optional, default 600)
    - h: max height in px (optional, default 220)
    """
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
        out_path = get_image_service().ensure_cached_file(product_url, max_w, max_h)
        if out_path and Path(out_path).exists():
            resp = send_file(out_path, conditional=True)
            resp.headers['Cache-Control'] = 'public, max-age=86400'
            return resp
        # If caching failed, return 404 instead of 500
        return jsonify({'error': 'image not available'}), 404
    except Exception as e:  # noqa: BLE001 - image pipeline touches many libs
        logger.error("Error serving image for %s: %s", product_url, e, exc_info=True)
        return jsonify({'error': 'image processing failed'}), 404
