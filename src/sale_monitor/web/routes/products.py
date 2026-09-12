"""Product CRUD and price-check endpoints."""
import csv
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from io import StringIO
from urllib.parse import urlparse

import requests
from flask import Blueprint, Response, current_app, jsonify, request

from sale_monitor.domain.models import Product
from sale_monitor.services.http_safety import safe_get
from sale_monitor.services.price_extractor import PriceExtractor
from sale_monitor.storage.config_store import get_base_currency
from sale_monitor.storage.json_state import delete_state_entry, mutate_state
from sale_monitor.utils import utcnow_iso
from sale_monitor.web.auth import require_api_key, require_api_key_for_reads
from sale_monitor.web.extensions import rate_limit
from sale_monitor.web.helpers import (
    config_file,
    get_check_service,
    get_ex_service,
    get_history,
    get_product_store,
    get_state_cache,
    invalidate_alerts_cache,
    paginate,
    safe_error,
    state_file,
)

logger = logging.getLogger(__name__)

bp = Blueprint('products', __name__)


def _parse_csv_list(val, current=None):
    if val is None:
        return current if current is not None else []
    if not val:
        return []
    if isinstance(val, list):
        return [s.strip() for s in val if isinstance(s, str) and s.strip()]
    return [s.strip() for s in str(val).split(',') if s.strip()]


@bp.route('/api/products')
@require_api_key_for_reads
def api_products():
    """Get all products with current state."""
    try:
        product_store = get_product_store()
        products = product_store.get_all()
        state = get_state_cache().get()
        base_currency = get_base_currency(config_file())

        # Reuse shared exchange rate service
        ex_service = get_ex_service()

        # Pre-fetch all needed exchange rates in one batch so individual
        # convert() calls below are served from memory cache.
        product_currencies = set()
        for p in products:
            sd = state.get(p.url, {})
            cur = (sd.get('currency') or getattr(p, 'currency', None) or 'CAD').upper()
            if cur != base_currency:
                product_currencies.add(cur)
        if product_currencies:
            ex_service.prefetch(product_currencies, base_currency)

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
                'scrape_url': getattr(p, 'scrape_url', None),
                'identifiers': state_data.get('identifiers', {}),
                'group': getattr(p, 'group', None),
                'tags': getattr(p, 'tags', []),
                'alert_rules': getattr(p, 'alert_rules', []),
                'notification_channels': getattr(p, 'notification_channels', []),
            })

        return jsonify(paginate(result))
    except (OSError, ValueError, sqlite3.Error) as e:
        return safe_error(e)


@bp.route('/api/product/toggle', methods=['POST'])
@require_api_key
def api_toggle_product():
    """Toggle product enabled status."""
    try:
        data = request.get_json()
        url = data.get('url')
        if not url:
            return jsonify({'error': 'URL required'}), 400

        product_store = get_product_store()
        product = product_store.get_by_url(url)
        if not product:
            return jsonify({'error': 'Product not found'}), 404

        new_enabled = not product.enabled
        product_store.update(url, enabled=new_enabled)
        invalidate_alerts_cache()

        return jsonify({'success': True, 'enabled': new_enabled})
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/product/check', methods=['POST'])
@require_api_key
def api_check_product():
    """Manually trigger price check for a product."""
    try:
        data = request.get_json()
        url = data.get('url')
        if not url:
            return jsonify({'error': 'URL required'}), 400

        product_store = get_product_store()
        product = product_store.get_by_url(url)
        if not product:
            return jsonify({'error': 'Product not found'}), 404

        service = get_check_service()
        result = service.check(product)
        if not result.success:
            return jsonify({'error': 'Failed to extract price'}), 500

        recs = []
        mutate_state(state_file(), lambda s: recs.append(service.apply_to_state(s, result)))
        rec = recs[0]

        return jsonify({
            'success': True,
            'price': result.price,
            'price_in_base': result.price_in_base,
            'currency': result.currency,
            'base_currency': result.base_currency,
            'timestamp': rec['last_checked'],
            'selector_source': result.selector_source,
            'currency_source': result.currency_source,
        })
    except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
        return safe_error(e)


@bp.route('/api/products/check-all', methods=['POST'])
@require_api_key
@rate_limit("2 per minute")
def api_check_all_products():
    """Trigger price check for all enabled products.

    Checks run in a small thread pool (the sequential version could exceed
    the gunicorn worker timeout).  Returns summary of successes / failures.
    """
    try:
        product_store = get_product_store()
        enabled = product_store.get_enabled()
        if not enabled:
            return jsonify({'success': True, 'updated': 0, 'failed': 0, 'message': 'No enabled products'}), 200

        history = get_history()
        service = get_check_service()
        base_currency = get_base_currency(config_file())

        valid = []
        failed = 0
        for p in enabled:
            product_url = (p.url or '').strip()
            if not product_url or not product_url.lower().startswith(('http://', 'https://')):
                # Invalid URL format; count as failed and skip
                history.record_price(p.url or 'invalid', p.name, None, status='failed', currency=p.currency or 'CAD')
                failed += 1
                continue
            valid.append(p)

        results = []
        if valid:
            max_workers = min(4, len(valid))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(service.check, p): p for p in valid}
                for future in as_completed(futures):
                    try:
                        results.append(future.result())
                    except Exception as e:  # noqa: BLE001 - worker isolation
                        p = futures[future]
                        logger.error("check-all: %s failed: %s", p.name, e)
                        history.record_price(p.url, p.name, None, status='failed',
                                             currency=getattr(p, 'currency', None) or 'CAD')
                        failed += 1

        successes = [r for r in results if r.success]
        failed += sum(1 for r in results if not r.success)

        if successes:
            def _apply_all(state):
                for r in successes:
                    service.apply_to_state(state, r)
            mutate_state(state_file(), _apply_all)

        invalidate_alerts_cache()
        return jsonify({
            'success': True,
            'updated': len(successes),
            'failed': failed,
            'base_currency': base_currency,
            'timestamp': utcnow_iso(),
        })
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/product/delete', methods=['POST'])
@require_api_key
@rate_limit("10 per minute")
def api_delete_product():
    """Delete a product."""
    try:
        data = request.get_json()
        url = data.get('url')
        if not url:
            return jsonify({'error': 'URL required'}), 400

        product_store = get_product_store()
        if not product_store.delete(url):
            return jsonify({'error': 'Product not found'}), 404

        # Remove from state.json and invalidate alerts cache
        try:
            delete_state_entry(state_file(), url)
            invalidate_alerts_cache()
        except OSError:
            pass

        return jsonify({'success': True})
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/products/auto-detect-all', methods=['POST'])
@require_api_key
@rate_limit("2 per minute")
def api_auto_detect_all():
    """Attempt to auto-detect price selectors for all products."""
    try:
        product_store = get_product_store()
        products = product_store.get_all()

        extractor = PriceExtractor(
            user_agent=current_app.config['USER_AGENT'],
            timeout=current_app.config['TIMEOUT'],
            max_retries=current_app.config['MAX_RETRIES']
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
                logger.error("Auto-detect failed for %s: %s", product.url, e)
                failed += 1

        return jsonify({
            'success': True,
            'successful': successful,
            'failed': failed
        })
    except (OSError, ValueError, sqlite3.Error, requests.exceptions.RequestException) as e:
        logger.error("Bulk auto-detect error: %s", e)
        return safe_error(e)


@bp.route('/api/product/fetch-info', methods=['POST'])
@require_api_key
@rate_limit("10 per minute")
def api_fetch_product_info():
    """Fetch product name from a URL by inspecting JSON-LD, og:title, or <title>."""
    import json as _json
    try:
        data = request.get_json()
        url = (data or {}).get('url', '').strip()
        if not url:
            return jsonify({'error': 'url is required'}), 400
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return jsonify({'error': 'URL must use http or https'}), 400

        resp = safe_get(url, headers={
            'User-Agent': current_app.config['USER_AGENT'],
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }, timeout=current_app.config['TIMEOUT'])
        if resp is None:
            return jsonify({'error': 'URL is not reachable from this server'}), 400
        if resp.status_code >= 400:
            return jsonify({'error': f'HTTP {resp.status_code}'}), 502
        html = resp.text

        name = None
        # 1) JSON-LD Product name
        for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
            try:
                ld = _json.loads(m.group(1))
                items = ld if isinstance(ld, list) else [ld]
                for item in items:
                    if isinstance(item, dict):
                        if item.get('@type') in ('Product', 'IndividualProduct') and item.get('name'):
                            name = item['name'].strip()
                            break
                        # Check @graph
                        for node in item.get('@graph', []):
                            if isinstance(node, dict) and node.get('@type') in ('Product', 'IndividualProduct') and node.get('name'):
                                name = node['name'].strip()
                                break
                    if name:
                        break
            except (_json.JSONDecodeError, ValueError, TypeError):
                continue
            if name:
                break

        # 2) og:title
        if not name:
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', html)
            if m:
                name = m.group(1).strip()

        # 3) <title> tag
        if not name:
            m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
            if m:
                name = m.group(1).strip()
                # Strip common suffixes like " | Store Name" or " - Store Name"
                name = re.split(r'\s*[\|–—\-]\s*(?=[^|–—\-]*$)', name)[0].strip()

        return jsonify({'name': name or ''})
    except requests.exceptions.RequestException as e:
        return jsonify({'error': str(e)}), 502
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/product/add', methods=['POST'])
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

        # Create product
        new_product = Product(
            name=data['name'],
            url=data['url'],
            target_price=target_price,
            discount_threshold=discount_threshold,
            selector=data.get('selector', ''),  # Default to empty string if not provided
            enabled=data.get('enabled', True),
            notification_cooldown_hours=cooldown_hours,
            scrape_url=(data.get('scrape_url') or '').strip() or None,
            group=data.get('group', '').strip() or None,
            tags=_parse_csv_list(data.get('tags')),
            alert_rules=_parse_csv_list(data.get('alert_rules')),
            notification_channels=_parse_csv_list(data.get('notification_channels')),
        )

        # Check for duplicate URL and add
        product_store = get_product_store()
        if product_store.get_by_url(new_product.url):
            return jsonify({'error': 'Product with this URL already exists'}), 400

        product_store.add(new_product)
        invalidate_alerts_cache()

        # Auto-check price synchronously so the UI shows it immediately
        price_result = None
        try:
            service = get_check_service()
            result = service.check(new_product)
            if result.success:
                mutate_state(state_file(), lambda s: service.apply_to_state(s, result))
                logger.info("Auto-checked new product '%s': $%s %s",
                            new_product.name, result.price, result.currency)
                price_result = {'price': float(result.price), 'currency': result.currency}
            else:
                logger.warning("Failed to auto-check price for new product '%s'", new_product.name)
        except Exception as e:  # noqa: BLE001 - auto-check must not fail the add
            logger.error("Error auto-checking price for new product '%s': %s", new_product.name, e)

        resp = {'success': True, 'product': {
            'name': new_product.name,
            'url': new_product.url,
            'enabled': new_product.enabled,
            'notification_cooldown_hours': new_product.notification_cooldown_hours
        }}
        if price_result:
            resp['price_check'] = price_result
        return jsonify(resp)
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/product/update', methods=['POST'])
@require_api_key
def api_update_product():
    """Update an existing product."""
    try:
        data = request.get_json()
        url = data.get('url')
        if not url:
            return jsonify({'error': 'URL required'}), 400

        product_store = get_product_store()
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

        raw_scrape_url = data.get('scrape_url', getattr(p, 'scrape_url', None))
        fields = {
            'name': data.get('name', p.name),
            'target_price': target_price,
            'discount_threshold': discount_threshold,
            'selector': data.get('selector', p.selector),
            'scrape_url': (raw_scrape_url.strip() or None) if isinstance(raw_scrape_url, str) else raw_scrape_url,
            'enabled': data.get('enabled', p.enabled),
            'notification_cooldown_hours': cooldown_hours,
            'group': data.get('group', p.group).strip() if data.get('group') is not None else p.group,
            'tags': _parse_csv_list(data.get('tags'), getattr(p, 'tags', [])),
            'alert_rules': _parse_csv_list(data.get('alert_rules'), getattr(p, 'alert_rules', [])),
            'notification_channels': _parse_csv_list(data.get('notification_channels'), getattr(p, 'notification_channels', [])),
        }
        product_store.update(url, **fields)
        invalidate_alerts_cache()

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
        return safe_error(e)


@bp.route('/api/products/bulk-import', methods=['POST'])
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

        product_store = get_product_store()
        existing_urls = product_store.urls()

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

        invalidate_alerts_cache()
        return jsonify({
            'success': True,
            'added': len(added),
            'skipped': len(skipped),
            'errors': errors,
            'added_names': added,
            'skipped_names': skipped,
        })
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/export/products')
@require_api_key_for_reads
def api_export_products():
    """Export all products as CSV."""
    try:
        from sale_monitor.storage.csv_products import CSV_COLUMNS
        product_store = get_product_store()
        products = product_store.get_all()
        output = StringIO()
        writer = csv.writer(output)
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
                getattr(p, 'scrape_url', None) or '',
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
        return safe_error(e)
