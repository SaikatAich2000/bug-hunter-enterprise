#!/usr/bin/env bash
# =============================================================================
#  restore.sh — Restore a dump made by backup.sh into the running database
#
#  ./scripts/restore.sh backups/bughunter-20260101T030000Z.dump
#  ./scripts/restore.sh --force <file>    (no confirmation prompt)
#
#  The app is stopped while the restore runs and started again afterwards. A
#  safety dump of the current database is taken first, so a restore can itself
#  be undone with the file it prints.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMPOSE_FILE="$ROOT/docker-compose.yml"
ENV_FILE="$ROOT/.env"
DB_CONTAINER="${DB_CONTAINER:-bugtracker_db}"

FORCE=false
if [[ "${1:-}" == "--force" ]]; then FORCE=true; shift; fi
DUMP="${1:-}"
[[ -n "$DUMP" && -f "$DUMP" ]] || { echo "usage: $0 [--force] <dump file>" >&2; exit 2; }

env_value() {
  [[ -f "$ENV_FILE" ]] || return 0
  { grep -E "^$1=" "$ENV_FILE" || true; } | tail -n 1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//'
}
DB_USER="$(env_value POSTGRES_USER)"; DB_USER="${DB_USER:-bugtracker}"
DB_NAME="$(env_value POSTGRES_DB)"; DB_NAME="${DB_NAME:-bugtracker}"

docker exec -i "$DB_CONTAINER" pg_restore --list > /dev/null < "$DUMP" \
  || { echo "[restore] $DUMP is not a readable pg_dump archive" >&2; exit 1; }

if [[ "$FORCE" != true ]]; then
  read -r -p "Replace ALL data in database '$DB_NAME' with $DUMP? Type 'restore' to continue: " answer
  [[ "$answer" == "restore" ]] || { echo "[restore] cancelled"; exit 1; }
fi

# Restore from a private copy: the safety backup's retention may prune the original.
COPY="$(mktemp)"
trap 'rm -f "$COPY"' EXIT
cp -- "$DUMP" "$COPY"

echo "[restore] taking a safety backup of the current data first"
"$ROOT/scripts/backup.sh"

echo "[restore] stopping the app"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" stop app

echo "[restore] restoring $DUMP"
docker exec -i "$DB_CONTAINER" pg_restore -U "$DB_USER" -d "$DB_NAME" \
  --clean --if-exists --no-owner --single-transaction < "$COPY"

echo "[restore] starting the app"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" start app
echo "[restore] done"
