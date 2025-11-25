#!/bin/bash
set -euo pipefail

APP_NAME="sale-monitor-next"
SERVER="root@tower"
REMOTE_APP_DIR="/mnt/user/appdata/$APP_NAME"
REMOTE_BACKUP_DIR="/mnt/user/Backups/${APP_NAME}_backups"

if [ -z "${1-}" ]; then
  echo "Usage: $0 <backup_filename>"
  echo "Example: $0 code_20251120_183045.tar.gz"
  echo ""
  echo "Available backups on server:"
  ssh "$SERVER" "ls -1 '$REMOTE_BACKUP_DIR'"
  exit 1
fi

BACKUP_FILE="$1"

echo "🧯 Rolling back $APP_NAME using backup: $BACKUP_FILE"

# Stop containers first so we don't mess with live files
ssh "$SERVER" "cd '$REMOTE_APP_DIR' && docker compose -f docker-compose.yml down"

# Restore code over current app dir (data/ is unaffected because it wasn’t in the tar)
ssh "$SERVER" "cd /mnt/user/appdata && \
  tar -xzf '${APP_NAME}_backups/${BACKUP_FILE}'"

# Start containers again
ssh "$SERVER" "cd '$REMOTE_APP_DIR' && docker compose -f docker-compose.yml up -d"

echo "✅ Rollback complete!"
