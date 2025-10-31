# Sale Monitor Next

A Python application that monitors product prices from online retailers and sends email notifications when prices drop. Features CSV-based product management, SQLite price history tracking, and continuous monitoring with configurable intervals.

## Features

### Core Functionality
- **CSV-based product management** - Easy editing in Excel/Sheets, no config.json editing
- **Email notifications** - SMTP with STARTTLS, configurable cooldown periods
- **Intelligent cooldown** - Notifies on price changes even during cooldown window
- **Price history tracking** - SQLite database stores all price checks with configurable retention
- **Continuous monitoring** - Scheduled checks at configurable intervals (minutes/hours/seconds)
- **Web dashboard** - Real-time monitoring at http://localhost:5000 with auto-refresh
- **Docker support** - Ready-to-deploy with docker-compose

### Query & Analysis
- **List products** - View all products with history
- **Show history** - View price history for specific products
- **Statistics** - Min/max/average prices, check counts
- **CSV export** - Export all history data
- **Price charts** - Visual price trends in web dashboard

## Quick Start

### Local Installation

### Local Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your SMTP settings

# Create products CSV
mkdir -p data
cat > data/products.csv <<'CSV'
name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours
Example Product,https://example.com/product,199.99,15,.price-selector,true,24
CSV

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

## Usage

### Web Dashboard

**Start the web server:**
```bash
PYTHONPATH=src python -m sale_monitor.web.app
```

Open http://localhost:5000 in your browser for:

**Dashboard (/):**
- View all products with current prices and status
- Search and filter products by name or status
- Quick toggle enable/disable monitoring
- Manual price check button for immediate updates
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
| `SMTP_SERVER` | - | SMTP server address (e.g., smtp.mail.me.com) |
| `SMTP_PORT` | 587 | SMTP port (587 for STARTTLS) |
| `SMTP_USERNAME` | - | SMTP authentication username |
| `SMTP_PASSWORD` | - | SMTP authentication password |
| `RECIPIENT_EMAIL` | - | Email address for notifications |
| `ENABLE_EMAIL_NOTIFICATIONS` | true | Enable/disable notifications |
| `NOTIFICATION_COOLDOWN_HOURS` | 24 | Default notification cooldown |
| `CHECK_INTERVAL` | 15m | Monitoring interval (e.g., '15m', '1h') |
| `HISTORY_RETENTION_DAYS` | 90 | Days to keep history (0 = forever) |
| `LOG_LEVEL` | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |

### Products CSV Format

```csv
name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours
Example Product,https://example.com/product,199.99,15,.price-selector,true,24
```

**Columns:**
- `name` - Product display name (required)
- `url` - Product page URL (required)
- `target_price` - Notify when price <= this value (optional)
- `discount_threshold` - Notify when discount >= this % (optional)
- `selector` - CSS selector for price element (required)
- `enabled` - true/false to enable/disable monitoring (default: true)
- `notification_cooldown_hours` - Hours between notifications (default: 24)

## Project Structure
```
sale-monitor-next
├── src/
│   └── sale_monitor/
│       ├── __init__.py
│       ├── cli/
│       │   ├── __init__.py
│       │   └── main.py
│       ├── domain/
│       │   ├── __init__.py
│       │   └── models.py
│       ├── services/
│       │   ├── __init__.py
│       │   ├── notifications.py
│       │   ├── price_extractor.py
│       │   └── scheduler.py
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── base.py
│       │   ├── csv_products.py
│       │   ├── file_lock.py
│       │   ├── json_state.py
│       │   ├── json_store.py
│       │   ├── price_history.py
│       │   └── sqlite_store.py
│       └── web/
│           ├── __init__.py
│           ├── app.py
│           ├── routes/
│           │   ├── __init__.py
│           │   └── products.py
│           └── templates/
│               ├── base.html
│               ├── index.html
│               └── product_detail.html
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_cli_cooldown.py
│   ├── test_json_state.py
│   ├── test_notifications.py
│   ├── test_price_extractor.py
│   └── test_price_history.py
├── data/
│   ├── products.csv
│   ├── state.json
│   └── history.db
├── .env.example
├── .gitignore
├── docker-compose.yml
├── DOCKER.md
├── Dockerfile
├── pyproject.toml
├── pytest.ini
├── requirements.txt
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

## Contribution
Contributions are welcome! Please submit a pull request or open an issue for any enhancements or bug fixes.

## License
This project is licensed under the MIT License. See the LICENSE file for more details.