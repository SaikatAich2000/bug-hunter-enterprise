#!/usr/bin/env bash
# =============================================================================
#  backup.sh — Dump the Bug Hunter database to backups/ (pg_dump custom format)
#
#  ./scripts/backup.sh               one timestamped dump, keeps the newest 14
#  BACKUP_KEEP=30 ./scripts/backup.sh
#  BACKUP_DIR=/mnt/backups ./scripts/backup.sh
#
#  Restore with ./scripts/restore.sh <file>. Copy dumps off this host too: a
#  backup on the same disk as the database does not survive losing that disk.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/.env"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-14}"
DB_CONTAINER="${DB_CONTAINER:-bugtracker_db}"

env_value() {
  # Last assignment of $1 in .env, without quotes; empty when unset.
  [[ -f "$ENV_FILE" ]] || return 0
  { grep -E "^$1=" "$ENV_FILE" || true; } | tail -n 1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}

DB_USER="$(env_value POSTGRES_USER)"; DB_USER="${DB_USER:-bugtracker}"
DB_NAME="$(env_value POSTGRES_DB)"; DB_NAME="${DB_NAME:-bugtracker}"

if ! docker inspect --format '{{.State.Running}}' "$DB_CONTAINER" 2>/dev/null | grep -qx true; then
  echo "[backup] $DB_CONTAINER is not running; nothing to back up." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/bughunter-$STAMP.dump"
PARTIAL="$TARGET.partial"

# Custom format: compressed, and pg_restore can restore all of it or single tables.
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc --no-owner > "$PARTIAL"
# A dump pg_restore can't list is not a backup.
docker exec -i "$DB_CONTAINER" pg_restore --list > /dev/null < "$PARTIAL"
mv "$PARTIAL" "$TARGET"
chmod 600 "$TARGET"
echo "[backup] wrote $TARGET ($(du -h "$TARGET" | cut -f1))"

# Retention: keep the newest $BACKUP_KEEP dumps.
mapfile -t OLD < <(ls -1t "$BACKUP_DIR"/bughunter-*.dump 2>/dev/null | tail -n +"$((BACKUP_KEEP + 1))")
for f in "${OLD[@]}"; do
  rm -f -- "$f"
  echo "[backup] removed old $f"
done
