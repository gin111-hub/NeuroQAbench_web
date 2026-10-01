#!/usr/bin/env bash
# Daily snapshot of the verify-submission SQLite db.
#
# Install via cron (00:30 UTC daily):
#   30 0 * * * /srv/x_to_skills/QAbench/human_verify_web/deploy/backup.sh
#
# SQLite .backup is safe to run while uvicorn is writing (uses a shared lock,
# no corruption risk). Keeps 30 days.

set -euo pipefail

BACKEND_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../backend" && pwd)"
DB="$BACKEND_DIR/db.sqlite"
OUT_DIR="$BACKEND_DIR/backups"
STAMP="$(date -u +%Y-%m-%d)"
OUT="$OUT_DIR/$STAMP.sqlite"

mkdir -p "$OUT_DIR"

if [[ ! -f "$DB" ]]; then
    echo "backup.sh: no db at $DB, nothing to do"
    exit 0
fi

sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
echo "backup.sh: wrote $OUT.gz"

# retention: 30 days
find "$OUT_DIR" -maxdepth 1 -name '*.sqlite.gz' -mtime +30 -delete
