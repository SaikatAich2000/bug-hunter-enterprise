#!/usr/bin/env bash
# Run pytest with coverage, then push the report to the local SonarQube via the
# sonar-scanner-cli Docker image. Env overrides: SONAR_HOST_URL (default
# http://localhost:9090) and SONAR_TOKEN (required if anonymous analysis is disabled).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info() { echo -e "${GREEN}[SONAR]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
abort() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

command -v docker >/dev/null 2>&1 || abort "docker is not installed"
docker info >/dev/null 2>&1 || abort "Docker daemon is not running"
command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1 \
  || abort "python is not on PATH"
PY=$(command -v python || command -v python3)

SONAR_HOST_URL="${SONAR_HOST_URL:-http://localhost:9090}"
SONAR_TOKEN="${SONAR_TOKEN:-}"

# sonar.projectVersion is not hardcoded in sonar-project.properties; it comes
# from APP_VERSION in .env so there is exactly one place to bump a release.
if [[ -z "${APP_VERSION:-}" ]]; then
  APP_VERSION="$(grep -m1 '^APP_VERSION=' "${ROOT}/.env" 2>/dev/null | cut -d= -f2- | tr -d '\r')"
fi
if [[ -z "${APP_VERSION:-}" ]]; then
  abort "APP_VERSION is not set in the environment or .env - add it (see .env.example)."
fi
info "Using APP_VERSION=${APP_VERSION} as sonar.projectVersion"

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

info "Running pytest with coverage..."
rm -f coverage.xml junit.xml

# Python 3.14 + pytest-cov: C-level finalizer ResourceWarnings bypass pyproject
# filterwarnings, so silence them at the interpreter level. Coverage data unaffected.
PREV_PY_WARNINGS="${PYTHONWARNINGS:-}"
export PYTHONWARNINGS=ignore

"$PY" -m pytest \
  --cov=app \
  --cov-report=xml:coverage.xml \
  --cov-report=term-missing:skip-covered \
  --junitxml=junit.xml \
  -q || warn "pytest exited non-zero — running scanner anyway so partial results land in SonarQube."

export PYTHONWARNINGS="${PREV_PY_WARNINGS}"

# Repo root is mounted as /usr/src so the scanner sees sonar-project.properties + reports.
info "Running sonar-scanner-cli via Docker..."
SCANNER_ARGS=(
  "-e" "SONAR_HOST_URL=${SONAR_HOST_URL}"
)
if [[ -n "${SONAR_TOKEN}" ]]; then
  SCANNER_ARGS+=("-e" "SONAR_TOKEN=${SONAR_TOKEN}")
fi

# host.docker.internal isn't always defined on Linux; rewrite localhost so the
# scanner container can reach the also-in-Docker SonarQube.
EFFECTIVE_HOST_URL="${SONAR_HOST_URL}"
if echo "${SONAR_HOST_URL}" | grep -qE '://(localhost|127\.0\.0\.1)(:|/|$)'; then
  if docker network inspect bridge >/dev/null 2>&1; then
    # Bridge gateway works everywhere without add-host=host.docker.internal:host-gateway.
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

# Git Bash/MSYS: convert /c/… to C:/… so Docker Desktop can mount it.
MOUNT_SRC="${ROOT}"
if command -v cygpath >/dev/null 2>&1; then
  MOUNT_SRC="$(cygpath -m "${ROOT}")"
fi

docker run --rm \
  "${SCANNER_ARGS[@]}" \
  -v "${MOUNT_SRC}:/usr/src" \
  -w /usr/src \
  sonarsource/sonar-scanner-cli:latest \
  -Dsonar.projectVersion="${APP_VERSION}"

info "Done. Browse results at ${SONAR_HOST_URL}/dashboard?id=Bug-Hunter-Enterprise"
