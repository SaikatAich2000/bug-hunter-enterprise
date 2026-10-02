#!/usr/bin/env bash
# =============================================================================
#  down.sh — Stop the Bug Hunter stack
# =============================================================================
set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[BUG-HUNTER]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }

# ── Parse flags ──────────────────────────────────────────────────────────────
WIPE_DB=false
CLEAN_DB=false
REMOVE_IMAGES=false
FORCE=false

usage() {
  echo ""
  echo "Usage: $0 [OPTIONS]"
  echo ""
  echo "  (no flags)       Stop containers, keep DB volume & image intact"
  echo "  --clean-db       Reset the database to its FIRST-LAUNCH state: deletes"
  echo "                   all work items/projects/users/audit rows but KEEPS the"
  echo "                   bootstrap admin (its .env credentials), the seeded"
  echo "                   default project and the workflow baseline. Prints a"
  echo "                   dry-run plan, then asks before deleting."
  echo "  --wipe-db        Also DELETE the bugtracker_pgdata volume (ALL DATA LOST,"
  echo "                   baseline included — the app re-seeds it from .env)"
  echo "  --remove-images  Also remove the built bugtracker_app image"
  echo "  --full-clean     Equivalent to --wipe-db --remove-images"
  echo "  --force          Skip the confirmation prompts (for automation)"
  echo ""
}

for arg in "$@"; do
  case $arg in
    --clean-db)       CLEAN_DB=true ;;
    --wipe-db)        WIPE_DB=true ;;
    --remove-images)  REMOVE_IMAGES=true ;;
    --full-clean)     WIPE_DB=true; REMOVE_IMAGES=true ;;
    --force)          FORCE=true ;;
    --help|-h)        usage; exit 0 ;;
    *)                echo -e "${RED}[ERROR]${NC} Unknown option: $arg"; usage; exit 1 ;;
  esac
done

# ── Optional: reset the data, keeping the first-launch baseline ───────────────
# Runs BEFORE `down` (the app image is still present and the db gets started on
# demand), and never drops the volume, so the bootstrap admin's credentials and
# the seeded project survive. `--wipe-db` below is the only destructive path.
if [[ "$CLEAN_DB" == true ]]; then
  info "Starting the database service for the cleanup..."
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d db

  info "Waiting for bugtracker_db to be healthy..."
  RETRIES=20
  until docker inspect --format='{{.State.Health.Status}}' bugtracker_db 2>/dev/null \
        | grep -qx "healthy"; do
    RETRIES=$((RETRIES - 1))
    [[ $RETRIES -le 0 ]] && { echo -e "${RED}[ERROR]${NC} bugtracker_db did not become healthy in time."; exit 1; }
    sleep 3
  done

  # --no-deps: the db is already up; the app service is only used as the image
  # that carries scripts/clean_db.py and the DATABASE_URL for this stack.
  CLEAN=(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" run --rm --no-deps -T app
         python scripts/clean_db.py)

  info "Planned cleanup (dry run — nothing deleted yet):"
  "${CLEAN[@]}" --dry-run || warn "Cleanup dry run could not complete (is the image built? run ./deploy.sh first)."

  if [[ "$FORCE" == true ]]; then
    warn "--force given: deleting without confirmation."
    CONFIRM="YES"
  elif ! read -rp "Delete this data now? The admin login, default project and workflow baseline stay. Type YES to confirm: " CONFIRM; then
    CONFIRM=""
  fi

  if [[ "$CONFIRM" == "YES" ]]; then
    "${CLEAN[@]}" --yes || warn "Cleanup did not complete."
    info "Database reset to the first-launch baseline. Users sign in again with the .env bootstrap credentials."
  else
    info "Skipped the database cleanup."
  fi
fi

# Only this Compose project's containers are stopped; other containers on the
# host are never touched.
info "Stopping Bug Hunter services only..."

# ── Stop & remove containers ──────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down
else
  docker compose -f "$COMPOSE_FILE" down
fi

# ── Optional: wipe DB volume ─────────────────────────────────────────────────
if [[ "$WIPE_DB" == true ]]; then
  warn "Removing bugtracker_pgdata volume — ALL DATABASE DATA WILL BE LOST."
  if [[ "$FORCE" == true ]]; then
    warn "--force given: skipping confirmation and deleting the DB volume NOW."
    CONFIRM="YES"
  else
    # Guard the read: on a non-TTY stdin (CI / piped) read returns non-zero at
    # EOF, which under `set -e` would abort the whole script BEFORE the
    # image-removal block below — so e.g. `--full-clean </dev/null` would
    # silently skip removing the image. Treat a failed read as "no confirmation"
    # (skip the destructive wipe) and keep going.
    if ! read -rp "Are you sure? Type YES to confirm: " CONFIRM; then
      CONFIRM=""
    fi
  fi
  if [[ "$CONFIRM" == "YES" ]]; then
    docker volume rm bugtracker_pgdata 2>/dev/null && info "Volume removed." \
      || warn "Volume not found or already removed."
  else
    info "Skipped volume removal."
  fi
fi

# ── Optional: remove built image ─────────────────────────────────────────────
if [[ "$REMOVE_IMAGES" == true ]]; then
  # Read the same APP_VERSION .env uses for the image tag (docker-compose.yml
  # requires it with no fallback), so the tag isn't a second hardcoded copy.
  APP_VERSION="$(grep -m1 '^APP_VERSION=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)"
  if [[ -z "$APP_VERSION" ]]; then
    warn "APP_VERSION is not set in $ENV_FILE - skipping image removal."
  else
    info "Removing bugtracker_app:${APP_VERSION} image..."
    docker rmi "bugtracker_app:${APP_VERSION}" 2>/dev/null && info "Image removed." \
      || warn "Image not found or already removed."
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        Bug Hunter stopped cleanly.           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
info "To restart Bug Hunter: ./deploy.sh"
echo ""

