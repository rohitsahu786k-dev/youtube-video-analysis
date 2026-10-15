#!/usr/bin/env bash
# Daily backup: consistent SQLite snapshot + market data + evidence. Add to cron: 15 3 * * * /opt/tvi/deploy/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${BACKUP_DIR:-./backups}"; KEEP="${BACKUP_KEEP:-14}"; STAMP=$(date +%Y%m%d-%H%M)
mkdir -p "$OUT"
python3 - <<PY
import sqlite3
s = sqlite3.connect("storage/intelligence.sqlite3"); d = sqlite3.connect("$OUT/db-$STAMP.sqlite3"); s.backup(d)
ok = d.execute("PRAGMA integrity_check").fetchone()[0]; d.close(); s.close()
if ok != "ok":
    raise SystemExit("Backup integrity check failed: " + ok)
PY
tar -czf "$OUT/files-$STAMP.tgz" storage/datasets storage/evidence 2>/dev/null || true
ls -1t "$OUT"/db-*.sqlite3 | tail -n +$((KEEP+1)) | xargs -r rm -f
ls -1t "$OUT"/files-*.tgz | tail -n +$((KEEP+1)) | xargs -r rm -f
echo "backup $STAMP done"
