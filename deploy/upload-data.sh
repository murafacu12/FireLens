#!/usr/bin/env bash
# Copy FIRELENS's data from this PC to the server, so production starts with everything loaded.
# Run from the repository root in Git Bash:   bash deploy/upload-data.sh ubuntu@<VM-IP> [~/key.pem]
#
# Copies a consistent snapshot of firelens.db (SQLite online backup, safe while the local API
# runs), the seasonal archive (data/baseline.db), the per-fire report cache (data/enrichment.db)
# and the land-cover / elevation tile cache (data/geo, optional: it re-downloads on demand).
# Training data (data/training) is not needed in production and is left out.
# Stop the API on the server first (docker compose stop api), then start it again afterwards.
set -euo pipefail

TARGET="${1:?usage: upload-data.sh user@host [ssh-key]}"
KEY="${2:-}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && SSH_OPTS+=(-i "$KEY")
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="FireLens/deploy/data"
PY="$ROOT/backend/.venv/Scripts/python"
[ -x "$PY" ] || PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || PY=python

SNAP="$ROOT/backups/upload-snapshot.db"
mkdir -p "$ROOT/backups"
echo "== Snapshot of firelens.db"
"$PY" - "$ROOT/firelens.db" "$SNAP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
ls -lh "$SNAP" | awk '{print "   " $5}'

ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p $REMOTE"
echo "== Database (compressed in transit)"
gzip -c "$SNAP" | ssh "${SSH_OPTS[@]}" "$TARGET" "gunzip -c > $REMOTE/firelens.db"
for f in baseline.db enrichment.db; do
  if [ -f "$ROOT/data/$f" ]; then
    echo "== data/$f"
    gzip -c "$ROOT/data/$f" | ssh "${SSH_OPTS[@]}" "$TARGET" "gunzip -c > $REMOTE/$f"
  fi
done
if [ -d "$ROOT/data/geo" ] && [ "${SKIP_GEO:-}" != "1" ]; then
  echo "== data/geo (tile cache, ~800 MB; SKIP_GEO=1 to skip)"
  tar -C "$ROOT/data" -czf - geo | ssh "${SSH_OPTS[@]}" "$TARGET" "tar -C $REMOTE -xzf -"
fi
rm -f "$SNAP"
# the container runs as uid 10001
ssh "${SSH_OPTS[@]}" "$TARGET" "sudo chown -R 10001 $REMOTE"
echo "Done. On the server: cd FireLens/deploy && docker compose up -d"
