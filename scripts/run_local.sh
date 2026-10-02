#!/usr/bin/env bash
# Run Bug Hunter locally without Docker (SQLite, http://127.0.0.1:8000).
#
# First run creates .venv (Python 3.12, via uv when available) with the pinned
# dependencies and .env with generated secrets; later runs just start the
# server. Log in with BOOTSTRAP_ADMIN_EMAIL / BOOTSTRAP_ADMIN_PASSWORD from .env.
#
#   ./scripts/run_local.sh              # port 8000
#   PORT=9000 RELOAD=1 ./scripts/run_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -x .venv/Scripts/python.exe ]; then
  py=.venv/Scripts/python.exe          # Windows (Git Bash)
else
  py=.venv/bin/python
fi

if [ ! -x "$py" ]; then
  echo "Creating .venv and installing dependencies (first run only)..."
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv --python 3.12
    [ -x .venv/Scripts/python.exe ] && py=.venv/Scripts/python.exe || py=.venv/bin/python
    # Pinned to the CI lockfile's versions (Linux-only entries don't apply elsewhere).
    uv pip install --python "$py" -r requirements.txt -r requirements-dev.txt -c requirements-dev-lock.txt
  else
    python3 -m venv .venv
    [ -x .venv/Scripts/python.exe ] && py=.venv/Scripts/python.exe || py=.venv/bin/python
    "$py" -m pip install -r requirements.txt -r requirements-dev.txt
  fi
fi

if [ ! -f .env ]; then
  "$py" scripts/gen_local_env_secrets.py
  echo "Created .env. Set APP_VERSION in it; the admin password is BOOTSTRAP_ADMIN_PASSWORD."
fi

export PYTHONUTF8=1
port="${PORT:-8000}"
args=(-m uvicorn app.main:app --host 127.0.0.1 --port "$port" --no-server-header)
if [ "${RELOAD:-0}" = "1" ]; then
  args+=(--reload --reload-dir app)
fi
echo "Bug Hunter: http://127.0.0.1:$port  (Ctrl+C to stop)"
exec "$py" "${args[@]}"
