#!/usr/bin/env bash
#
# Production SQLite backup script (runs on the VPS via SSH from the
# backup workflow). Takes a consistent snapshot of every configured
# database file via sqlite3's online .backup command, gzips it into
# the configured backup directory, and prunes backups older than the
# retention window.
#
# Required env (all from the workflow):
#   APP_DIR            absolute path to the production app directory
#                      (used to locate database.db unless APP_DATABASE_PATH
#                       is set in the app's .env)
#
# Optional env:
#   BACKUP_DIR             defaults to /var/backups/app
#   BACKUP_RETENTION_DAYS  defaults to 14
#   EXTRA_DB_PATHS         space-separated list of additional .db files
#                          to back up (e.g. cloud_budget store, if it
#                          lives outside APP_DIR/database.db)
#
# The script is idempotent and safe to run while uvicorn is serving:
# sqlite3 .backup takes a copy without exclusive locking the source.

set -euo pipefail

APP_DIR="${APP_DIR:?APP_DIR is required}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/app}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
EXTRA_DB_PATHS="${EXTRA_DB_PATHS:-}"

# Resolve the main DB path. Honor APP_DATABASE_PATH from the app .env if set,
# otherwise fall back to the documented default of database.db inside APP_DIR.
MAIN_DB_PATH=""
if [[ -f "$APP_DIR/.env" ]]; then
  MAIN_DB_PATH=$(grep -E '^APP_DATABASE_PATH=' "$APP_DIR/.env" | tail -n1 | cut -d= -f2- || true)
fi
if [[ -z "$MAIN_DB_PATH" ]]; then
  MAIN_DB_PATH="$APP_DIR/database.db"
elif [[ "$MAIN_DB_PATH" != /* ]]; then
  # Relative path in the .env is resolved against APP_DIR, mirroring how
  # the FastAPI process resolves it on startup.
  MAIN_DB_PATH="$APP_DIR/$MAIN_DB_PATH"
fi

sudo mkdir -p "$BACKUP_DIR"
sudo chown "$(id -u):$(id -g)" "$BACKUP_DIR"

timestamp=$(date -u +"%Y%m%d-%H%M%S")
declare -i success_count=0
declare -i failure_count=0

backup_one() {
  local src="$1"
  if [[ ! -f "$src" ]]; then
    echo "warn: $src not found, skipping" >&2
    return 0
  fi
  local name
  name=$(basename "$src")
  local out="$BACKUP_DIR/${name%.db}-${timestamp}.sqlite.gz"
  local tmp
  tmp=$(mktemp --suffix=.sqlite)
  trap 'rm -f "$tmp"' RETURN

  # sqlite3 .backup is the official online-backup API. It coordinates with
  # any other readers/writers via the WAL so the resulting file is a
  # transactionally consistent snapshot, even under concurrent writes.
  # Some VPS images have Python's sqlite3 module but not the sqlite3 CLI.
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '$tmp'"
  else
    python3 - "$src" "$tmp" <<'PY'
import sqlite3
import sys

src, tmp = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as source:
    with sqlite3.connect(tmp) as target:
        source.backup(target)
PY
  fi

  if [[ -s "$tmp" ]]; then
    gzip -c "$tmp" > "$out"
    rm -f "$tmp"
    echo "ok: $src -> $out ($(stat -c%s "$out") bytes)"
    success_count+=1
  else
    rm -f "$tmp"
    echo "error: backup of $src failed" >&2
    failure_count+=1
  fi
}

backup_one "$MAIN_DB_PATH"
for extra in $EXTRA_DB_PATHS; do
  backup_one "$extra"
done

# Prune old backups.
find "$BACKUP_DIR" -type f -name '*.sqlite.gz' -mtime "+$BACKUP_RETENTION_DAYS" -print -delete || true

echo "summary: success=$success_count failure=$failure_count retained_window_days=$BACKUP_RETENTION_DAYS"

if (( failure_count > 0 )); then
  exit 1
fi
