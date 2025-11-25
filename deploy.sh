#!/bin/bash
set -euo pipefail

APP_NAME="sale-monitor-next"
SERVER="root@tower"
REMOTE_APP_DIR="/mnt/user/appdata/$APP_NAME"
REMOTE_BACKUP_DIR="/mnt/user/Backups/${APP_NAME}_backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

echo "🚀 Deploying $APP_NAME to $SERVER..."

echo "📦 Creating pre-deploy backup on server (code only, no data/)..."
ssh "$SERVER" "mkdir -p '$REMOTE_BACKUP_DIR' && \
  tar -czf '${REMOTE_BACKUP_DIR}/code_${TIMESTAMP}.tar.gz' \
    --exclude='data' \
    --exclude='.git' \
    --exclude='.conda' \
    --exclude='.venv' \
    -C /mnt/user/appdata \
    '$APP_NAME'"

echo "✅ Backup created: ${REMOTE_BACKUP_DIR}/code_${TIMESTAMP}.tar.gz"

echo "🧹 Pruning old backups (keeping last 5)..."
ssh "$SERVER" "
  cd '$REMOTE_BACKUP_DIR' && \
  ls -1t | tail -n +6 | xargs -r rm -f
"
echo "✅ Old backups pruned"

echo "📤 Syncing files with rsync..."
rsync -avz \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.conda' \
  --exclude '.venv' \
  --exclude 'data/*.json' \
  --exclude 'data/*.db*' \
  --exclude 'data/images/' \
  --exclude 'data/products.csv' \
  --exclude 'data/config.json' \
  /path/to/sale-monitor-next/ \
  "$SERVER:$REMOTE_APP_DIR/"

echo "✅ Files synced"

echo "🔄 Restarting container..."
ssh "$SERVER" "docker compose -f '$REMOTE_APP_DIR/docker-compose.yml' restart"

echo "✅ Deployment complete!"
echo ""
echo "💡 Tip: Code backup stored as:"
echo "   $REMOTE_BACKUP_DIR/code_${TIMESTAMP}.tar.gz"
echo "💡 DB/files in data/ are NOT touched by deploys or backups."
echo "   To sync database, use: scp data/history.db $SERVER:$REMOTE_APP_DIR/data/"
