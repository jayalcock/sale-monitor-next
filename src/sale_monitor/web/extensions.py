"""Flask extensions shared across blueprints (rate limiter)."""
import logging

logger = logging.getLogger(__name__)

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address

    # NOTE: memory:// storage is per-process, so with N gunicorn workers the
    # effective limit is N x the configured value.  Point storage_uri at a
    # shared backend (e.g. redis) if strict limits ever matter.
    limiter = Limiter(
        get_remote_address,
        default_limits=["60 per minute"],
        storage_uri="memory://",
    )
except ImportError:
    limiter = None  # type: ignore[assignment]
    logger.warning("flask-limiter not installed; rate limiting disabled")


def rate_limit(limit_string):
    """Conditional rate limit decorator (no-op when limiter unavailable)."""
    def decorator(f):
        if limiter is not None:
            return limiter.limit(limit_string)(f)
        return f
    return decorator


def init_app(app):
    if limiter is not None:
        limiter.init_app(app)
