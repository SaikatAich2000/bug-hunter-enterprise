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
REMOVE_IMAGES=false

usage() {
  echo ""
  echo "Usage: $0 [OPTIONS]"
  echo ""
  echo "  (no flags)       Stop containers, keep DB volume & image intact"
  echo "  --wipe-db        Also DELETE the bugtracker_pgdata volume (ALL DATA LOST)"
  echo "  --remove-images  Also remove the built bugtracker_app image"
  echo "  --full-clean     Equivalent to --wipe-db --remove-images"
  echo ""
}

for arg in "$@"; do
  case $arg in
    --wipe-db)        WIPE_DB=true ;;
    --remove-images)  REMOVE_IMAGES=true ;;
    --full-clean)     WIPE_DB=true; REMOVE_IMAGES=true ;;
    --help|-h)        usage; exit 0 ;;
    *)                echo -e "${RED}[ERROR]${NC} Unknown option: $arg"; usage; exit 1 ;;
  esac
done

# ── Safety: never touch pmis-postgres ────────────────────────────────────────
info "Stopping Bug Hunter services only (pmis-* containers are untouched)..."

# ── Stop & remove containers ──────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down
else
  docker compose -f "$COMPOSE_FILE" down
fi

# ── Optional: wipe DB volume ─────────────────────────────────────────────────
if [[ "$WIPE_DB" == true ]]; then
  warn "Removing bugtracker_pgdata volume — ALL DATABASE DATA WILL BE LOST."
  read -rp "Are you sure? Type YES to confirm: " CONFIRM
  if [[ "$CONFIRM" == "YES" ]]; then
    docker volume rm bugtracker_pgdata 2>/dev/null && info "Volume removed." \
      || warn "Volume not found or already removed."
  else
    info "Skipped volume removal."
  fi
fi

# ── Optional: remove built image ─────────────────────────────────────────────
if [[ "$REMOVE_IMAGES" == true ]]; then
  info "Removing bugtracker_app image..."
  docker rmi bugtracker_app:2.4 2>/dev/null && info "Image removed." \
    || warn "Image not found or already removed."
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        Bug Hunter stopped cleanly.           ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
info "pmis-postgres and all pmis-* services are still running."
info "To restart Bug Hunter: ./deploy.sh"
echo ""

