"""Authentication and security middleware for the Flask web app."""
import functools
import logging
import os

from flask import request, jsonify

logger = logging.getLogger(__name__)


def _get_api_key():
    """Return configured API key, or None if auth is disabled."""
    return os.getenv('API_KEY') or None


def require_api_key(f):
    """Decorator: reject requests without a valid API key.

    Key is read from the API_KEY env var.  When API_KEY is unset or empty,
    the decorator is a no-op (auth disabled).  The key can be supplied via
    the ``X-API-Key`` header or the ``api_key`` query parameter.
    """
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        expected = _get_api_key()
        if not expected:
            return f(*args, **kwargs)

        provided = request.headers.get('X-API-Key') or request.args.get('api_key')
        if not provided or provided != expected:
            logger.warning("Unauthorized API request to %s from %s",
                           request.path, request.remote_addr)
            return jsonify({'error': 'Unauthorized'}), 401

        return f(*args, **kwargs)
    return decorated


def require_api_key_for_reads(f):
    """Like require_api_key, but only enforced when API_KEY_READ_REQUIRED=1."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if os.getenv('API_KEY_READ_REQUIRED', '0').strip().lower() in ('1', 'true', 'yes'):
            return require_api_key(f)(*args, **kwargs)
        return f(*args, **kwargs)
    return decorated
