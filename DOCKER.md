# Docker Quick Start Guide

## Overview

The Docker setup uses a single container that runs two processes via supervisord:
1. **Web (gunicorn)** - Web dashboard for managing and viewing products
2. **Monitor (scheduler)** - Background price monitoring service

Both processes share the same data volume, so changes made in the web UI are immediately reflected in the monitoring loop.

## Prerequisites
- Docker and Docker Compose installed
- `.env` file configured (see `.env` example below)
- `data/products.csv` with products to monitor

## Setup

1. **Create your `.env` file** (if not already present):
```bash
cp .env.example .env
# Edit .env with your SMTP settings
```

2. **Create your products CSV**:
```bash
mkdir -p data
cat > data/products.csv <<'CSV'
name,url,target_price,discount_threshold,selector,enabled,notification_cooldown_hours
Example Product,https://example.com/product,199.99,15,.price-selector,true,24
CSV
```

3. **Build and run**:
```bash
docker compose up --build
```

4. **Access the web dashboard**:
Open http://localhost:5050 in your browser

## Usage

### Run in background (detached mode)
```bash
docker compose up -d
```

### View logs
```bash
# Both services
docker compose logs -f

# Monitoring service only
docker compose logs -f sale-monitor

# Web dashboard only
docker compose logs -f sale-monitor-web
```

### Access web dashboard
After starting with `docker compose up -d`:
- Open http://localhost:5050
- Dashboard: View all products with live prices
- Alerts: See products hitting targets
- Manage: Add/edit/delete products via web UI
- Export: Download price history CSV

### Stop the containers
```bash
docker compose down
```

### Restart after updating CSV (or use web UI instead!)
```bash
# After editing data/products.csv locally OR via web UI
docker compose restart
```

### Rebuild after code changes
```bash
docker compose up --build
```

## Services Architecture

### Processes
- Web: Flask app served by gunicorn (container port 5000, exposed on host 5050)
- Monitor: Python scheduler that checks prices on an interval (default 15 minutes)
- Real-time dashboard with auto-refresh
- Product management (add/edit/delete)
- Price history charts and statistics
- Manual price check functionality
- Export history to CSV

Both services share the `./data` volume for seamless data synchronization.

## File Sync Workflow

### Option 1: Use Web UI (Recommended)
- Access http://localhost:5000/manage
 - Or http://localhost:5050/manage when running via Docker
- Add/edit/delete products directly in the browser
- Changes automatically saved to `data/products.csv`
- Monitoring service picks up changes on next check

### Option 2: Local to Server (Mac → Server)
```bash
# 1. Edit data/products.csv locally
vim data/products.csv

# 2. Copy to server
scp data/products.csv user@server:/path/to/sale-monitor-next/data/

# 3. On server, restart containers
ssh user@server "cd /path/to/sale-monitor-next && docker compose restart"
```

### Option 3: Git-based workflow
```bash
# On Mac
git add data/products.csv
git commit -m "Update products"
git push

# On server
git pull
docker compose restart
```

## Volume Mounts

The docker-compose.yml mounts:
- `./data:/app/data` - Shared data directory (persistent)
  - `products.csv` - Product definitions
  - `state.json` - Current prices and check times
  - `history.db` - SQLite price history database
- `./src:/app/src:ro` - Source code (read-only, for development)

Both containers access the same data volume, enabling:
- Web UI to modify products.csv
- Monitoring service to read products.csv and update state
- Web UI to display current state and history

## Environment Variables

All settings from `.env` are passed to the container. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `ENABLE_EMAIL_NOTIFICATIONS` | true | Enable/disable email notifications |
| `SMTP_SERVER` | - | SMTP server address |
| `SMTP_PORT` | 587 | SMTP port (587 for STARTTLS) |
| `SMTP_USERNAME` | - | SMTP authentication username |
| `SMTP_PASSWORD` | - | SMTP authentication password |
| `RECIPIENT_EMAIL` | - | Email address to send notifications to |
| `NOTIFICATION_COOLDOWN_HOURS` | 24 | Default cooldown between notifications |
| `CHECK_INTERVAL` | 15m | How often to check prices (e.g., '15m', '1h', '30s') |

### Scheduling

The container runs continuously by default, checking prices every 15 minutes (configurable via `CHECK_INTERVAL`).

**Format examples:**
- `15m` - Every 15 minutes
- `1h` - Every 1 hour
- `30s` - Every 30 seconds

**For a one-time run** (exit after checking):
```bash
docker compose run --rm sale-monitor python -m sale_monitor.cli.main --products-csv /app/data/products.csv --state-file /app/data/state.json
```

## Troubleshooting

### Container exits immediately
```bash
docker compose logs sale-monitor
# Check for CSV parsing errors or missing files
```

### Web dashboard not accessible
```bash
# Check if host port 5050 is already in use
lsof -i :5050

# Check web service logs
docker compose logs sale-monitor-web

# Ensure service is running
docker compose ps
```

### CSV not found
```bash
# Ensure data/products.csv exists locally before running
ls -la data/products.csv
```

### Permission errors with volumes
```bash
# Ensure data directory is readable
chmod -R 755 data/
```

### Can't add products via web UI
```bash
# Check data directory is writable
ls -la data/
# Web service needs write access to products.csv
```

### Test without Docker first
```bash
# Run monitoring locally
export PYTHONPATH=src
python -m sale_monitor.cli.main --products-csv data/products.csv --state-file data/state.json

# Run web dashboard locally
export PYTHONPATH=src
python -m sale_monitor.web.app
```

## Production Deployment

For production, consider these changes to docker-compose.yml:

1. **Remove source mount** (bake code into image):
```yaml
volumes:
  - ./data:/app/data
  # Remove: - ./src:/app/src:ro
```

2. **Web runs with gunicorn by default**
The docker-compose configuration already uses gunicorn for the web process.

3. **Set resource limits**:
```yaml
deploy:
  resources:
    limits:
      cpus: '0.5'
      memory: 512M
```

4. **Use environment-specific configs**:
```yaml
environment:
  - FLASK_DEBUG=0
  - LOG_LEVEL=WARNING
```

5. **Healthcheck**
The container includes a Docker healthcheck that probes the web root URL.
