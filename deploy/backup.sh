#!/usr/bin/env bash
# Daily backup of the FIRELENS database (run by cron on the server; see setup-vm.sh).
# Uses SQLite's online backup, so it is consistent while the API keeps writing.
# Keeps the last 7 days in deploy/backups/. If the OCI CLI is set up and OCI_BUCKET is set,
# also uploads each copy to Oracle Object Storage (20 GB free).
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/backups"
STAMP="$(date -u +%Y%m%d)"
mkdir -p "$OUT"

cd "$DIR"
docker compose exec -T api python - <<'PY'
import sqlite3
src = sqlite3.connect("file:/data/firelens.db?mode=ro", uri=True)
dst = sqlite3.connect("/data/backup-tmp.db")
src.backup(dst)
dst.close()
src.close()
PY

gzip -c "$DIR/data/backup-tmp.db" > "$OUT/firelens-$STAMP.db.gz"
docker compose exec -T api rm -f /data/backup-tmp.db
echo "$(date -u +%FT%TZ) backup $OUT/firelens-$STAMP.db.gz ($(du -h "$OUT/firelens-$STAMP.db.gz" | cut -f1))"

# keep 7 days
ls -1t "$OUT"/firelens-*.db.gz | tail -n +8 | xargs -r rm -f

if [ -n "${OCI_BUCKET:-}" ] && command -v oci >/dev/null; then
  oci os object put --bucket-name "$OCI_BUCKET" --file "$OUT/firelens-$STAMP.db.gz" --force >/dev/null
  echo "   uploaded to bucket $OCI_BUCKET"
fi
