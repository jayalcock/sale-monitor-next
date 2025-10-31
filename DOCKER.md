# Docker Quick Start Guide

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

## Usage

### Run in background (detached mode)
```bash
docker compose up -d
```

### View logs
```bash
docker compose logs -f sale-monitor
```

### Stop the container
```bash
docker compose down
```

### Restart after updating CSV
```bash
# After editing data/products.csv locally
docker compose restart
```

### Rebuild after code changes
```bash
docker compose up --build
```

## File Sync Workflow

### Local to Server (Mac → Server)
```bash
# 1. Edit data/products.csv locally
vim data/products.csv

# 2. Copy to server
scp data/products.csv user@server:/path/to/sale-monitor-next/data/

# 3. On server, restart container
ssh user@server "cd /path/to/sale-monitor-next && docker compose restart"
```

### Alternative: Git-based workflow
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
- `./data:/app/data` - Products CSV and state.json (persistent)
- `./src:/app/src:ro` - Source code (read-only, for development)

The container automatically reads from `/app/data/products.csv` and writes state to `/app/data/state.json`.

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

### Test without Docker first
```bash
# Run locally to verify setup
export PYTHONPATH=src
python -m sale_monitor.cli.main --products-csv data/products.csv --state-file data/state.json
```

## Production Deployment

For production, remove the source mount from docker-compose.yml:
```yaml
volumes:
  - ./data:/app/data
  # Remove: - ./src:/app/src:ro
```

This bakes the code into the image at build time instead of mounting it.
