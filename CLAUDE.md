# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Sale Monitor is a Python price monitoring application with a Flask web dashboard. It continuously checks product prices from online retailers, tracks history in SQLite, and sends notifications (email/Discord/Slack) when prices drop or meet alert thresholds.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run one-time price check
PYTHONPATH=src python -m sale_monitor.cli.main --products-csv data/products.csv --state-file data/state.json

# Run continuous monitoring (every 15 minutes)
PYTHONPATH=src python -m sale_monitor.cli.main --products-csv data/products.csv --state-file data/state.json --every 15m

# Run web dashboard (http://localhost:5000)
PYTHONPATH=src python -m sale_monitor.web.app

# Docker (http://localhost:5050)
docker compose up --build

# Run all tests
pytest

# Run a single test file
pytest tests/test_price_extractor.py

# Formatting and linting
black src tests
isort src tests
mypy src
```

## Architecture

**Entry points:**
- `src/sale_monitor/cli/main.py` — CLI orchestrator: reads products, extracts prices in parallel (ThreadPoolExecutor, 4 workers), records history, evaluates alerts, sends notifications
- `src/sale_monitor/web/app.py` — Flask app factory with route registration, mtime-cached state reader

**Layer structure:**
- `domain/models.py` — Product dataclass
- `services/` — Business logic: `price_extractor.py` (fetch + CSS selector extraction), `auto_detector.py` (60+ hardcoded selectors for major retailers), `notifications.py` (SMTP with retry), `exchange_rates.py` (currency conversion), `webhooks.py` (Discord/Slack)
- `storage/` — Persistence: `product_store.py` and `price_history.py` (SQLite with WAL mode), `json_state.py` (transient cooldown state), `csv_products.py` (import/export), `config_store.py` (settings JSON), `migrations.py` (schema versioning), `file_lock.py` (fcntl-based)
- `web/templates/` — Jinja2 templates for dashboard, manage, alerts, product detail, settings, compare views
- `web/auth.py` — Optional API key authentication

**Data flow:** Products live in SQLite (source of truth). CSV is import/export only. Price checks write to `price_history` table. Transient notification cooldown state stored in `data/state.json`. Exchange rates cached in SQLite.

**Deployment:** Docker container runs supervisord managing two processes: gunicorn (web) and the CLI monitor loop. Data persisted via volume mount at `./data`.

## Key Design Decisions

- SQLite WAL mode enables concurrent web reads + CLI writes
- Web app uses file mtime checks to avoid re-parsing state.json on every request
- Price extraction tries user CSS selector first, then falls back to auto-detection
- Currency detected from page (JSON-LD, meta tags, Shopify data), falls back to CSV value, then base currency config (default CAD)
- Per-product alert rules: target, discount, any_change, price_drop, below_avg
- Per-product notification channel overrides and cooldown settings
