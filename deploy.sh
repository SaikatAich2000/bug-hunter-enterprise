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

# ── Live-data safety ──────────────────────────────────────────────────────────
# This script does NOT touch the named volume `bugtracker_pgdata` that holds
# your Postgres data. The schema migration on startup is additive only:
# init_db() adds missing tables, columns and indexes and leaves everything else
# alone. Existing rows are not deleted. Sessions stay valid as long as
# SESSION_SECRET is unchanged.
info "Live-data safety: bugtracker_pgdata volume will NOT be touched by this script."

# ── Base-image pre-pull (resilient to transient Docker Hub timeouts) ─────────
# The default base image is python:3.12-slim. We retry the pull a few times
# so a flaky link to docker.io doesn't fail the whole deploy; if every retry
# times out we tell the user clearly what to do (proxy / mirror / preload).
BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}"
info "Pre-pulling base image: ${BASE_IMAGE}"
PULL_RETRIES=3
PULL_OK=0
for attempt in $(seq 1 ${PULL_RETRIES}); do
  if docker pull "${BASE_IMAGE}" 2>&1 | tail -3; then
    PULL_OK=1
    break
  fi
  warn "Pull attempt ${attempt}/${PULL_RETRIES} failed; retrying in 5s..."
  sleep 5
done
if [[ "${PULL_OK}" -ne 1 ]]; then
  # If the image is already present locally we can still build offline.
  if docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    warn "Could not refresh ${BASE_IMAGE} from the registry, but a cached copy is present locally — using it."
  else
    warn "Could not pull ${BASE_IMAGE} and no cached copy is present."
    warn "This is a NETWORK problem (Docker Hub unreachable from this host),"
    warn "not a Bug Hunter problem. Your database is not affected."
    warn ""
    warn "Try one of:"
    warn "  1. Check outbound connectivity to auth.docker.io / registry-1.docker.io"
    warn "  2. Configure a Docker daemon HTTP proxy in /etc/docker/daemon.json:"
    warn "       { \"proxies\": { \"http-proxy\": \"http://proxy:port\", \"https-proxy\": \"http://proxy:port\" } }"
    warn "     then restart docker (systemctl restart docker)."
    warn "  3. Use an internal registry mirror by overriding BASE_IMAGE:"
    warn "       BASE_IMAGE=mirror.internal/python:3.12-slim ./deploy.sh"
    warn "  4. Pre-load the image from a machine with network access:"
    warn "       docker save python:3.12-slim | gzip > python-3.12-slim.tgz"
    warn "       (move the file to this host, then)"
    warn "       zcat python-3.12-slim.tgz | docker load"
    warn "       ./deploy.sh"
    abort "Base image unavailable — aborting before the build."
  fi
fi

# ── Build & deploy ────────────────────────────────────────────────────────────
# Layer cache is honoured: COPY-driven invalidation still triggers a fresh
# build whenever app code or requirements.txt change, but pip-install +
# apt-install layers reuse the cache when nothing relevant changed. This
# also means a successful deploy survives transient registry blips on the
# next run. To force a full clean rebuild, run `BUILD_CLEAN=1 ./deploy.sh`.
info "Building application image..."
BUILD_ARGS=()
if [[ "${BUILD_CLEAN:-0}" == "1" ]]; then
  warn "BUILD_CLEAN=1 set — forcing --no-cache rebuild."
  BUILD_ARGS+=("--no-cache")
fi
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" \
  build --build-arg "BASE_IMAGE=${BASE_IMAGE}" "${BUILD_ARGS[@]}"

info "Starting services (db first, then app)..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --remove-orphans

# ── Health wait ───────────────────────────────────────────────────────────────
info "Waiting for bugtracker_db to be healthy..."
RETRIES=20
# grep -qx (whole-line match): a plain `grep -q healthy` ALSO matches the literal
# "unhealthy" status, declaring a failing DB ready. -qx requires the status to be
# exactly "healthy".
until docker inspect --format='{{.State.Health.Status}}' bugtracker_db 2>/dev/null \
      | grep -qx "healthy"; do
  RETRIES=$((RETRIES - 1))
  [[ $RETRIES -le 0 ]] && abort "bugtracker_db did not become healthy in time."
  sleep 3
done

info "Waiting for bugtracker_app to be healthy..."
# The image HEALTHCHECK calls /api/health, which returns 503 while the database
# is unreachable, so "healthy" means the app is actually serving. Allow for the
# 30 s start period plus a few check intervals. -qx: "unhealthy" must not match.
RETRIES=60
until docker inspect --format='{{.State.Health.Status}}' bugtracker_app 2>/dev/null \
      | grep -qx "healthy"; do
  RETRIES=$((RETRIES - 1))
  if [[ $RETRIES -le 0 ]]; then
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" logs --tail=40 app || true
    abort "bugtracker_app did not become healthy in time (last log lines above)."
  fi
  sleep 3
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║        Bug Hunter deployed successfully!     ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════╝${NC}"
echo ""
info "App  →  http://localhost:8765  (or this host's address, port 8765)"
info "DB   →  127.0.0.1:55432  (host-local admin access only)"
echo ""
info "To view logs:   docker compose -f $COMPOSE_FILE logs -f"
info "To stop:        ./down.sh"
echo ""
