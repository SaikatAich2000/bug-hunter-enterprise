# --- Bug Hunter — production-style image, intentionally small ---
#
# BASE_IMAGE is overridable so deployments behind a corporate proxy or
# air-gapped network can point at an internal registry mirror without
# editing this file:
#
#   BASE_IMAGE=mirror.internal/python:3.12-slim ./deploy.sh
#
# Default is the public Docker Hub tag.
#
# Python dependencies are installed from the hash-locked requirements-lock.txt.
# For fully reproducible builds, also pin the base image by DIGEST and refresh
# it via Dependabot/renovate, e.g.
#   BASE_IMAGE=python:3.12-slim@sha256:<digest> ./deploy.sh
# It stays a floating tag by default so the out-of-the-box build doesn't break
# when the upstream digest rotates.
ARG BASE_IMAGE=python:3.12-slim

# ---------------------------------------------------------------------------
# Stage: frontend-build
# Compiles frontend/src into app/static using a throwaway Node image. This
# means app/static is rebuilt fresh from source on every image build — the
# server itself never needs npm/node installed. Vite's outDir already points
# at ../app/static (see frontend/vite.config.js), so we hand it a copy of the
# committed app/static (favicon, icon, fonts, vendor/) and let the build
# overwrite index.html/login.html/reset.html/assets/ in place.
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /repo
COPY frontend ./frontend
COPY app/static ./app/static
WORKDIR /repo/frontend
RUN npm ci && npm run build

FROM ${BASE_IMAGE} AS base

# Don't write .pyc files, flush logs immediately, no pip version-check chatter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

# psycopg[binary] ships its own libpq, so we don't need build-essential
# or libpq-dev. Keep the image lean — just curl for the healthcheck. curl is
# deliberately unpinned: Debian replaces point releases in place, so a pinned
# version would break every build after the next security update. The upgrade
# applies Debian security fixes published after the base image was cut (the
# python:3.12-slim tag lags them by days to weeks).
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer caches across code changes.
# requirements-lock.txt pins the exact audited runtime set; requirements.txt
# stays the human-maintained source of truth (and the fallback if the lock is
# ever missing from the build context).
COPY requirements-lock.txt ./
RUN pip install --require-hashes -r requirements-lock.txt

# Copy application code
COPY app ./app

# Operator scripts (e.g. clean_db.py, invoked by down.sh --clean-db). Small,
# secret-free, and the build context already excludes tests/, secrets/ and .env.
COPY scripts ./scripts

# Overwrite app/static with the freshly-built frontend from the
# frontend-build stage, so the image never ships a stale bundle.
COPY --from=frontend-build /repo/app/static ./app/static

# Run as a non-root user with a fixed numeric UID/GID, so orchestrators that
# enforce runAsNonRoot can verify it without resolving a user name. 1000 is the
# UID the unpinned useradd already produced, so a host-side secrets/ file that
# is readable today (e.g. mode 0600 owned by uid 1000) stays readable.
RUN groupadd --gid 1000 appuser \
 && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER 1000:1000

EXPOSE 8000

# Container-level healthcheck hitting the app's /api/health endpoint, which now
# returns HTTP 503 (not 200) when the database is unreachable — so `curl -fsS`
# correctly reports the container unhealthy while the DB is down, instead of
# masking a degraded app. start-period gives the DB + first boot time to come up
# before failures count against the retry budget. Shell form is deliberate:
# Docker defines only exit codes 0/1, so curl's own codes are mapped to 1.
# hadolint ignore=DL3025
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# --no-server-header: uvicorn adds "Server: uvicorn" below the ASGI app, where
# the security-headers middleware cannot remove it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
