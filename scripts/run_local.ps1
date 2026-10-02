<#
.SYNOPSIS
    Run Bug Hunter locally without Docker (SQLite, http://127.0.0.1:8000).

.DESCRIPTION
    On first run this creates .venv (Python 3.12, via uv when available) with
    the pinned dependencies, and creates .env with generated secrets. Later
    runs just start the server. The login is BOOTSTRAP_ADMIN_EMAIL /
    BOOTSTRAP_ADMIN_PASSWORD from .env.

.EXAMPLE
    .\scripts\run_local.ps1            # start on port 8000
    .\scripts\run_local.ps1 -Reload    # auto-restart on backend code changes
    .\scripts\run_local.ps1 -Port 9000
#>
param(
    [int]$Port = 8000,
    [switch]$Reload
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Creating .venv and installing dependencies (first run only)..."
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv venv .venv --python 3.12
        if ($LASTEXITCODE -ne 0) { throw "uv venv failed" }
        # Pinned to the CI lockfile's versions (Linux-only entries don't apply here).
        uv pip install --python $py -r requirements.txt -r requirements-dev.txt -c requirements-dev-lock.txt
    } else {
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "python -m venv failed (install Python 3.12 or uv)" }
        & $py -m pip install -r requirements.txt -r requirements-dev.txt
    }
    if ($LASTEXITCODE -ne 0) { throw "Dependency install failed" }
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    & $py scripts/gen_local_env_secrets.py
    Write-Host "Created .env. Set APP_VERSION in it; the admin password is BOOTSTRAP_ADMIN_PASSWORD."
}

# Windows consoles default to a legacy code page; keep log output UTF-8.
$env:PYTHONUTF8 = "1"
$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port", "--no-server-header")
if ($Reload) { $uvicornArgs += @("--reload", "--reload-dir", "app") }
Write-Host "Bug Hunter: http://127.0.0.1:$Port  (Ctrl+C to stop)"
& $py @uvicornArgs
