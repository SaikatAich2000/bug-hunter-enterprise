#!/usr/bin/env bash
# =============================================================================
#  deploy.sh — Build & start the Bug Hunter stack (fully isolated)
# =============================================================================
set -euo pipefail

COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
ENV_EXAMPLE="$(cd "$(dirname "$0")" && pwd)/.env.example"

# ── Colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[BUG-HUNTER]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
abort()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Pre-flight checks ─────────────────────────────────────────────────────────
command -v docker        >/dev/null 2>&1 || abort "docker is not installed."
docker info              >/dev/null 2>&1 || abort "Docker daemon is not running."
command -v docker        >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 \
  || abort "docker compose (v2) is required."

# ── .env guard ───────────────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    warn ".env not found — copying from .env.example"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
  else
    abort ".env file is missing. Create one from .env.example before deploying."
  fi
fi

# ── Safety: confirm we are NOT touching the pmis-postgres container ───────────
if docker ps --format '{{.Names}}' | grep -q '^pmis-postgres$'; then
  info "Detected running pmis-postgres — Bug Hunter uses its OWN isolated db (bugtracker_db). No conflict."
fi

# ── Live-data safety ──────────────────────────────────────────────────────────
# This script does NOT touch the named volume `bugtracker_pgdata` that holds
# your Postgres data. The schema migration on startup is additive only:
# init_db() -> Base.metadata.create_all() creates any tables that don't yet
# exist (in v3.1, the new `sessions` table) and leaves everything else alone.
# Existing rows are not modified. Existing session cookies stay valid.
info "Live-data safety: bugtracker_pgdata volume will NOT be touched by this script."

# ── Build & deploy ────────────────────────────────────────────────────────────
info "Building application image..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --no-cache

info "Starting services (db first, then app)..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --remove-orphans

# ── Health wait ───────────────────────────────────────────────────────────────
info "Waiting for bugtracker_db to be healthy..."
RETRIES=20
until docker inspect --format='{{.State.Health.Status}}' bugtracker_db 2>/dev/null \
      | grep -q "healthy"; do
  RETRIES=$((RETRIES - 1))
  [[ $RETRIES -le 0 ]] && abort "bugtracker_db did not become healthy in time."
  sleep 3
done

info "Waiting for bugtracker_app to be running..."
RETRIES=20
until docker inspect --format='{{.State.Status}}' bugtracker_app 2>/dev/null \
      | grep -q "running"; do
  RETRIES=$((RETRIES - 1))
  [[ $RETRIES -le 0 ]] && abort "bugtracker_app did not start in time."
  sleep 3
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        Bug Hunter deployed successfully!     ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
info "App  →  http://$(hostname -I | awk '{print $1}'):8765"
info "DB   →  localhost:55432  (isolated, NOT shared with pmis-postgres)"
echo ""
info "To view logs:   docker compose -f $COMPOSE_FILE logs -f"
info "To stop:        ./down.sh"
echo ""

