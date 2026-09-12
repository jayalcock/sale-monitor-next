# Sale Monitor Next

A Python application that monitors product prices from online retailers and sends email notifications when prices drop. Features CSV-based product management, SQLite price history tracking, and continuous monitoring with configurable intervals.

## Features

### Core Functionality
- **CSV-based product management** - Easy editing in Excel/Sheets, no config.json editing
- **Email notifications** - SMTP with STARTTLS, configurable cooldown periods
- **Intelligent cooldown** - Notifies on price changes even during cooldown window
- **Price history tracking** - SQLite database stores all price checks with configurable retention
- **Continuous monitoring** - Scheduled checks at configurable intervals (minutes/hours/seconds)
- **Web dashboard** - Real-time monitoring at http://localhost:5050 (Docker) or http://localhost:5000 (local) with auto-refresh
- **Breakage alerts** - Email when a product's price check keeps failing (site redesign / selector rot detection)
- **Docker support** - Ready-to-deploy with docker-compose

### Query & Analysis
- **List products** - View all products with history
- **Show history** - View price history for specific products
- **Statistics** - Min/max/average prices, check counts
- **CSV export** - Export all history data
- **Price charts** - Visual price trends in web dashboard

## Quick Start

### Local Installation

```bash
# Install dependencies (add requirements-dev.txt for tests/linting)
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your SMTP settings

# Create products CSV (example)
mkdir -p data
cp data/products.example.csv data/products.csv

# Run once
PYTHONPATH=src python -m sale_monitor.cli.main \
  --products-csv data/products.csv \
  --state-file data/state.json

# Run continuously (every 15 minutes)
PYTHONPATH=src python -m sale_monitor.cli.main \
  --products-csv data/products.csv \
  --state-file data/state.json \
  --every 15m
```

### Docker Deployment

See [DOCKER.md](DOCKER.md) for complete Docker setup guide.

```bash
# Quick start
docker compose up -d

# View logs
docker compose logs -f

# Restart after updating CSV
docker compose restart
```

#### Unraid integration (WebUI and icon)

This project includes Docker labels for Unraid to surface the WebUI link and a custom icon.

- WebUI label (already set): `net.unraid.docker.webui=http://[IP]:[PORT:5050]`
- Icon label (served by the app): `net.unraid.docker.icon=http://[IP]:[PORT:5050]/static/img/icon.png`

To use a custom icon from this repository:
1. Place your icon file at `src/sale_monitor/web/static/img/icon.png` (PNG recommended).
2. Rebuild and redeploy the container so the icon is packaged into the image.
3. Hard refresh the Unraid Docker page (Ctrl/Cmd+Shift+R) to see the new icon.

Alternative (hosted icon): after pushing to GitHub, you can point the icon label to your repo instead of the running container:

```
net.unraid.docker.icon=https://raw.githubusercontent.com/<user>/<repo>/<branch>/src/sale_monitor/web/static/img/icon.png
```

## Usage

### Web Dashboard

**Start the web server:**
```bash
PYTHONPATH=src python -m sale_monitor.web.app
```

Open http://localhost:5000 in your browser for:

**Dashboard (/):**
- Trends chart at the top (7/30/90 days)
- Compact, scalable table with columns: Product, Merchant, Current Price, Status, Actions
- Sort by Product, Merchant, or Price; filter by Status; search by Product name
- Mobile responsive: actions collapse to a dropdown on small screens
- Quick toggle Enable/Disable monitoring; Manual "Check" to refresh a single product
- Auto-refreshes every 60 seconds

**Alerts Page (/alerts):**
- See all products that hit price targets
- View products with significant discounts
- Quick "Buy Now" links for deals
- Auto-refreshes to stay current

**Management Page (/manage):**
- Add new products with full configuration
- Edit existing product settings
- Delete products from monitoring
- Configure targets, thresholds, and cooldowns

**Product Detail Pages:**
- Interactive price history charts (30 days)
- Statistics: min/max/avg prices, check counts
- Current status and configuration

**Export:**
- Download complete price history as CSV
- Accessible from navbar → 📥 Export

The dashboard reads from `data/products.csv` and `data/state.json`, displaying real-time price data.

### Price Monitoring

**One-time check:**
```bash
python -m sale_monitor.cli.main --products-csv data/products.csv
```

**Continuous monitoring:**
```bash
# Every 15 minutes
python -m sale_monitor.cli.main --products-csv data/products.csv --every 15m

# Every 1 hour
python -m sale_monitor.cli.main --products-csv data/products.csv --every 1h
```

### Query History

**List all products:**
```bash
python -m sale_monitor.cli.main --list-products
```

**Show price history:**
```bash
python -m sale_monitor.cli.main --show-history "Product Name" --days 30
```

**Show statistics:**
```bash
python -m sale_monitor.cli.main --show-stats "Product Name" --days 30
```

**Export to CSV:**
```bash
python -m sale_monitor.cli.main --export-csv history_export.csv
```

## Configuration

### Environment Variables (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `SMTP_SERVER` | - | SMTP server address (e.g., smtp.example.com) |
| `SMTP_PORT` | 587 | SMTP port (587 for STARTTLS) |
| `SMTP_USERNAME` | - | SMTP authentication username |
| `SMTP_PASSWORD` | - | SMTP authentication password |
| `RECIPIENT_EMAIL` | - | Email address for notifications |
| `ENABLE_EMAIL_NOTIFICATIONS` | true | Enable/disable notifications |
| `NOTIFICATION_COOLDOWN_HOURS` | 24 | Default notification cooldown |
| `CHECK_INTERVAL` | 15m | Monitoring interval (e.g., '15m', '1h') |
| `HISTORY_RETENTION_DAYS` | 90 | Days to keep history (0 = forever) |
| `LOG_LEVEL` | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `PREFER_DETECTED_CURRENCY` | 1 | When 1/true (default), prefer currency detected from the product page HTML over the CSV-configured currency. Set to 0/false to always use the configured currency. |
| `FAILURE_ALERT_CONSECUTIVE` | 3 | Email an alert when a product's price check fails this many times in a row (selector rot / site redesign detection). 0 disables. |
| `API_KEY` | - | When set, write API endpoints require this key in the `X-API-Key` header. Save the key on the dashboard Settings page so the UI sends it automatically. |
| `API_KEY_READ_REQUIRED` | 0 | When 1/true, read endpoints require the API key too. |

### API Authentication

Setting `API_KEY` protects all state-changing endpoints. The dashboard stores
the key in your browser's local storage (Settings → Dashboard API Key) and
attaches it to every request as the `X-API-Key` header. External scripts must
send the same header — query parameters are not accepted, since they leak
into access logs.

Notification settings saved on the Settings page (SMTP password, webhook
URLs) are written to `data/config.json`, which is git-ignored — don't commit
it.

### Products CSV Format

```csv
name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours,selector_source
Example Product,https://example.com/product,199.99,15,,true,24,
```

**Columns:**
- `name` - Product display name (required)
- `url` - Product page URL (required)
- `target_price` - Notify when price <= this value (optional)
- `discount_threshold` - Notify when discount >= this % (optional)
- `selector` - CSS selector for price element (optional; leave blank to auto-detect)
- `enabled` - true/false to enable/disable monitoring (default: true)
- `notification_cooldown_hours` - Hours between notifications (default: 24)
- `selector_source` - One of `manual`, `auto`, or `bookmarklet` (optional; runtime may override)

Note: The dashboard derives the Merchant column from the product URL (domain), so no extra CSV column is required.

## Project Structure
```
sale-monitor-next
├── src/
│   └── sale_monitor/
│       ├── cli/
│       │   └── main.py            # CLI orchestrator + scheduler
│       ├── domain/
│       │   └── models.py          # Product dataclass
│       ├── services/
│       │   ├── price_check.py     # shared check pipeline (CLI + web)
│       │   ├── price_extractor.py # fetch + selector/JSON-LD extraction
│       │   ├── auto_detector.py   # per-retailer selector heuristics
│       │   ├── exchange_rates.py  # currency conversion + caching
│       │   ├── notifications.py   # SMTP (sale + failure alerts)
│       │   ├── webhooks.py        # Discord/Slack
│       │   ├── product_images.py  # image discovery/resize/cache + warmup
│       │   └── http_safety.py     # SSRF-safe fetching
│       ├── storage/
│       │   ├── product_store.py   # products table (source of truth)
│       │   ├── price_history.py   # price_history table (WAL mode)
│       │   ├── json_state.py      # transient state.json (locked writes)
│       │   ├── config_store.py    # settings JSON
│       │   ├── csv_products.py    # CSV import/export
│       │   ├── migrations.py      # versioned schema migrations
│       │   └── file_lock.py       # fcntl-based lock
│       ├── web/
│       │   ├── app.py             # Flask app factory
│       │   ├── auth.py            # optional X-API-Key auth
│       │   ├── helpers.py         # caches, pagination
│       │   ├── comparison.py      # competitive grouping
│       │   ├── extensions.py      # rate limiter
│       │   ├── routes/            # blueprints: pages, products, history,
│       │   │                      #   alerts, compare, settings, health,
│       │   │                      #   images, purchases
│       │   ├── templates/
│       │   └── static/
│       ├── logging_config.py
│       └── utils.py
├── tests/
├── data/                          # runtime data (git-ignored)
│   ├── config.example.json
│   ├── products.example.csv
│   └── state.example.json
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── requirements.txt               # runtime deps (Docker installs these)
├── requirements-dev.txt           # + pytest, mypy, pylint, stubs
└── README.md
```

## Installation
1. Clone the repository:
   ```
   git clone <repository-url>
   cd sale-monitor-next
   ```

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up your environment variables by copying `.env.example` to `.env` and filling in the necessary values.

4. Create your products CSV file in `data/products.csv` with the products you want to monitor.

## Usage
To run the application, use the command line interface:
```
python -m src.sale_monitor.cli.main
```

You can also run the application with scheduling or debug specific features using the available command-line arguments.

### Currency handling

Prices are stored with their native currency as scraped and the UI computes and displays an approximate value in your configured base currency. Some retailers serve geolocated prices (for example, CAD in Canada and USD in the U.S.). To prevent mislabeling, the backend prefers currency detected from the page (JSON‑LD/meta/Shopify tags or heuristics). If detection is missing, it falls back to a host heuristic, then to the CSV-configured currency, and finally to CAD. The source of the chosen currency is exposed as `currency_source` in `/api/products`, and the UI shows a small warning badge when it wasn’t detected from the page.

To force the CSV-configured currency regardless of detection, set:

```
PREFER_DETECTED_CURRENCY=0
```

## Contribution
Contributions are welcome! Please submit a pull request or open an issue for any enhancements or bug fixes.

## License
This project is licensed under the MIT License. See the LICENSE file for more details.