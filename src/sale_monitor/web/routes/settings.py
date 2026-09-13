"""Settings, notification config, and exchange-rate endpoints."""
import logging

from flask import Blueprint, jsonify, request

from sale_monitor.storage.config_store import (get_base_currency, load_config,
                                               load_notification_config,
                                               save_config,
                                               save_notification_config)
from sale_monitor.web.auth import require_api_key, require_api_key_for_reads
from sale_monitor.web.extensions import rate_limit
from sale_monitor.web.helpers import (config_file, currencies_in_use,
                                      get_ex_service, safe_error)

logger = logging.getLogger(__name__)

bp = Blueprint('settings', __name__)


@bp.route('/api/config', methods=['GET'])
@require_api_key_for_reads
def api_get_config():
    cfg = load_config(config_file())
    return jsonify({'base_currency': cfg.get('base_currency', 'CAD')})


@bp.route('/api/config/base-currency', methods=['POST'])
@require_api_key
def api_set_base_currency():
    try:
        data = request.get_json() or {}
        new_cur = str(data.get('base_currency', '')).upper().strip()
        if not new_cur or len(new_cur) != 3 or not new_cur.isalpha():
            return jsonify({'error': 'Invalid currency code'}), 400
        # Persist
        cfg = load_config(config_file())
        cfg['base_currency'] = new_cur
        save_config(config_file(), cfg)
        from flask import current_app
        current_app.config['BASE_CURRENCY'] = new_cur
        return jsonify({'success': True, 'base_currency': new_cur})
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/settings/notifications')
@require_api_key_for_reads
def api_get_notification_settings():
    """Return notification config with password masked."""
    try:
        cfg = load_notification_config(config_file())
        # Mask SMTP password
        smtp = cfg.get('smtp', {})
        if smtp.get('password'):
            smtp['password'] = '********'
        return jsonify(cfg)
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/settings/notifications', methods=['POST'])
@rate_limit("10 per minute")
@require_api_key
def api_save_notification_settings():
    """Save notification config. Accepts full notifications object."""
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Expected JSON object'}), 400
        # If password is masked placeholder, preserve the existing one
        existing = load_notification_config(config_file())
        smtp = data.get('smtp', {})
        if isinstance(smtp, dict) and smtp.get('password') == '********':
            smtp['password'] = existing.get('smtp', {}).get('password', '')
        save_notification_config(config_file(), data)
        return jsonify({'status': 'saved'})
    except (OSError, ValueError) as e:
        return safe_error(e)


@bp.route('/api/settings/notifications/test', methods=['POST'])
@rate_limit("5 per minute")
@require_api_key
def api_test_notification():
    """Send a test notification to a specified channel."""
    try:
        data = request.get_json(force=True)
        channel = data.get('channel', 'smtp')

        if channel == 'smtp':
            cfg = load_notification_config(config_file())
            smtp = cfg.get('smtp', {})
            if not smtp.get('server') or not smtp.get('to_email'):
                return jsonify({'error': 'SMTP not configured'}), 400
            from sale_monitor.services.notifications import (
                NotificationManager, SmtpConfig)
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
            cfg = load_notification_config(config_file())
            webhooks = cfg.get('webhooks', [])
            target_wh = None
            for wh in webhooks:
                if wh.get('name') == channel or wh.get('type') == channel:
                    target_wh = wh
                    break
            if not target_wh or not target_wh.get('url'):
                return jsonify({'error': f'Webhook "{channel}" not found or has no URL'}), 400
            from sale_monitor.services.webhooks import (DiscordWebhookNotifier,
                                                        SlackWebhookNotifier)
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
    except Exception as e:  # noqa: BLE001 - notification backends raise many types
        logger.error("Test notification failed: %s", e, exc_info=True)
        return safe_error(e)


@bp.route('/api/rates/refresh', methods=['POST'])
@require_api_key
def api_refresh_rates():
    """Force-refresh exchange rates from the API.

    Fetches fresh rates for every currency conversions depend on and
    reports genuine failure: the old implementation went through
    ``get_rate``, whose stale-cache fallback made a dead API look like a
    successful refresh.
    """
    try:
        ex_service = get_ex_service()
        base_currency = get_base_currency(config_file())
        bases = currencies_in_use(base_currency)
        if not bases:
            # Nothing to convert; still refresh one common base as a probe
            bases = {'USD' if base_currency != 'USD' else 'CAD'}
        ex_service.clear_cache()
        results = ex_service.refresh(bases)
        failed = sorted(b for b, ok in results.items() if not ok)
        if failed:
            return jsonify({'error': f'Failed to fetch rates for: {", ".join(failed)}'}), 502
        sample_base = sorted(results)[0]
        rate = ex_service.get_rate(sample_base, base_currency)
        return jsonify({
            'success': True,
            'refreshed': sorted(results),
            'sample_rate': f'{sample_base}/{base_currency} = {rate}',
        })
    except Exception as e:  # noqa: BLE001
        return safe_error(e, 'Rate refresh failed')
