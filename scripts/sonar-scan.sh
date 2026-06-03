#!/usr/bin/env bash
# =============================================================================
#  scripts/sonar-scan.sh
# -----------------------------------------------------------------------------
#  Run pytest with coverage, then push the report into the locally-running
#  SonarQube via the official sonar-scanner-cli Docker image.
#
#  Usage:
#      ./scripts/sonar-scan.sh                       # uses defaults
#      SONAR_HOST_URL=http://localhost:9000 \
#      SONAR_TOKEN=sqp_xxxxxxxxxxxx \
#          ./scripts/sonar-scan.sh
#
#  Defaults (override via env or arg):
#      SONAR_HOST_URL = http://localhost:9000
#      SONAR_TOKEN    = (none — SonarQube will reject the scan if anonymous
#                       analysis is disabled; create a project token in
#                       My Account → Security → Generate Tokens)
#
#  This script:
#    1. Sanity-checks Docker is up and SonarQube is reachable.
#    2. Runs pytest with --cov so coverage.xml + junit.xml are produced.
#    3. Runs sonar-scanner-cli (Docker) against the local SonarQube.
#
#  Database safety: the only things this script writes to disk are
#  coverage.xml, junit.xml and .scannerwork/ (all gitignored). No database
#  access, no schema changes, no container restarts.
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# --- Colours ----------------------------------------------------------------
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[SONAR]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
abort() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# --- Pre-flight -------------------------------------------------------------
command -v docker >/dev/null 2>&1 || abort "docker is not installed"
docker info >/dev/null 2>&1 || abort "Docker daemon is not running"
command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1 \
  || abort "python is not on PATH"
PY=$(command -v python || command -v python3)

SONAR_HOST_URL="${SONAR_HOST_URL:-http://localhost:9000}"
SONAR_TOKEN="${SONAR_TOKEN:-}"

if ! curl -sf -o /dev/null -m 5 "${SONAR_HOST_URL}/api/system/status"; then
  abort "Can't reach SonarQube at ${SONAR_HOST_URL}. Is the container up? Override with SONAR_HOST_URL=…"
fi
info "SonarQube is reachable at ${SONAR_HOST_URL}"

if [[ -z "${SONAR_TOKEN}" ]]; then
  warn "No SONAR_TOKEN set — the scan will be submitted anonymously."
  warn "If your SonarQube instance has 'Force user authentication' enabled"
  warn "(default in recent versions) the scan WILL fail with 401."
  warn "Generate a token at: ${SONAR_HOST_URL}/account/security/"
fi

# --- Coverage + test report -------------------------------------------------
info "Running pytest with coverage..."
rm -f coverage.xml junit.xml

# Silence Python 3.14 + pytest-cov gzip / sqlite finalizer ResourceWarnings
# (they bypass pyproject.toml's filterwarnings because they come from the
# C-level unraisablehook). Doesn't affect coverage data.
PREV_PY_WARNINGS="${PYTHONWARNINGS:-}"
export PYTHONWARNINGS=ignore

"$PY" -m pytest \
  --cov=app \
  --cov-report=xml:coverage.xml \
  --cov-report=term-missing:skip-covered \
  --junitxml=junit.xml \
  -q || warn "pytest exited non-zero — running scanner anyway so partial results land in SonarQube."

export PYTHONWARNINGS="${PREV_PY_WARNINGS}"

# --- Sonar scan via Docker --------------------------------------------------
# Use the official scanner image. The repo root is mounted as /usr/src so the
# scanner sees sonar-project.properties + the generated reports.
info "Running sonar-scanner-cli via Docker..."
SCANNER_ARGS=(
  "-e" "SONAR_HOST_URL=${SONAR_HOST_URL}"
)
if [[ -n "${SONAR_TOKEN}" ]]; then
  SCANNER_ARGS+=("-e" "SONAR_TOKEN=${SONAR_TOKEN}")
fi

# On Linux, host.docker.internal isn't always defined. If the user's
# SONAR_HOST_URL points at localhost, rewrite it for the scanner container
# so the scanner inside Docker can reach the SonarQube also-in-Docker.
EFFECTIVE_HOST_URL="${SONAR_HOST_URL}"
if echo "${SONAR_HOST_URL}" | grep -qE '://(localhost|127\.0\.0\.1)(:|/|$)'; then
  if docker network inspect bridge >/dev/null 2>&1; then
    # Prefer the Docker bridge gateway — works on every platform without
    # requiring add-host=host.docker.internal:host-gateway.
    GATEWAY=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true)
    if [[ -n "${GATEWAY}" ]]; then
      EFFECTIVE_HOST_URL=$(echo "${SONAR_HOST_URL}" | sed -E "s#://(localhost|127\.0\.0\.1)#://${GATEWAY}#")
      info "Rewriting SONAR_HOST_URL for the scanner container: ${EFFECTIVE_HOST_URL}"
      SCANNER_ARGS=(
        "-e" "SONAR_HOST_URL=${EFFECTIVE_HOST_URL}"
      )
      if [[ -n "${SONAR_TOKEN}" ]]; then
        SCANNER_ARGS+=("-e" "SONAR_TOKEN=${SONAR_TOKEN}")
      fi
    fi
  fi
fi

docker run --rm \
  "${SCANNER_ARGS[@]}" \
  -v "${ROOT}:/usr/src" \
  sonarsource/sonar-scanner-cli:latest

info "Done. Browse results at ${SONAR_HOST_URL}/dashboard?id=Bug_Hunter"
