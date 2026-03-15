"""Centralized logging configuration.

Usage:
    from sale_monitor.logging_config import setup_logging
    setup_logging()          # auto-detects TTY vs JSON
    setup_logging("json")    # force JSON
    setup_logging("text")    # force human-readable
"""
import logging
import os
import sys


def setup_logging(fmt: str | None = None, level: str | None = None) -> None:
    """Configure root logger with structured (JSON) or human-readable output.

    Args:
        fmt:   "json", "text", or None (auto-detect from LOG_FORMAT env / TTY).
        level: e.g. "DEBUG", "INFO". Falls back to LOG_LEVEL env, then INFO.
    """
    log_level = getattr(logging, (level or os.getenv("LOG_LEVEL", "INFO")).upper(), logging.INFO)

    fmt = fmt or os.getenv("LOG_FORMAT", "").lower() or None
    if fmt is None:
        fmt = "text" if sys.stderr.isatty() else "json"

    root = logging.getLogger()
    root.setLevel(log_level)
    # Remove existing handlers to avoid duplicates on re-init
    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)

    if fmt == "json":
        try:
            from pythonjsonlogger import jsonlogger
            formatter = jsonlogger.JsonFormatter(
                "%(asctime)s %(name)s %(levelname)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level"},
            )
        except ImportError:
            # Fallback: simple JSON-ish format without dependency
            formatter = logging.Formatter(
                '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
            )
    else:
        formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    handler.setFormatter(formatter)
    root.addHandler(handler)
