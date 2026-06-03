# =============================================================================
#  scripts/sonar-scan.ps1
# -----------------------------------------------------------------------------
#  Windows / PowerShell version of scripts/sonar-scan.sh.
#
#  Usage:
#      .\scripts\sonar-scan.ps1
#
#  Override via env vars (set in the same shell BEFORE invoking):
#      $env:SONAR_HOST_URL = "http://localhost:9000"
#      $env:SONAR_TOKEN    = "sqp_xxxxxxxxxxxx"
#      .\scripts\sonar-scan.ps1
#
#  Database safety: same as the bash version - only writes coverage.xml,
#  junit.xml, and .scannerwork/ (all gitignored). No DB access.
# =============================================================================
$ErrorActionPreference = "Stop"

# --- Locate repo root (parent of this script) ------------------------------
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info  { param([string]$msg) Write-Host "[SONAR] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

# --- Pre-flight ------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Abort "Docker is not on PATH. Install Docker Desktop and re-open this shell."
}
try { docker info *> $null } catch { Abort "Docker daemon is not running. Start Docker Desktop." }

$python = $null
foreach ($cmd in @("python", "py", "python3")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { $python = $cmd; break }
}
if (-not $python) { Abort "Python is not on PATH. Install Python 3.12 and re-open this shell." }

if (-not $env:SONAR_HOST_URL) { $env:SONAR_HOST_URL = "http://localhost:9000" }

# Reachability check
try {
    $resp = Invoke-WebRequest -Uri "$($env:SONAR_HOST_URL)/api/system/status" -TimeoutSec 5 -UseBasicParsing
    if ($resp.StatusCode -ne 200) { throw "non-200" }
} catch {
    Abort "Can't reach SonarQube at $($env:SONAR_HOST_URL). Is the container up? Try: docker start sonarqube"
}
Info "SonarQube is reachable at $($env:SONAR_HOST_URL)"

if (-not $env:SONAR_TOKEN) {
    Warn "No SONAR_TOKEN set - the scan will be submitted anonymously."
    Warn "If your SonarQube has 'Force user authentication' enabled (default"
    Warn "on recent versions), the scan WILL fail with 401."
    Warn "Generate a token at: $($env:SONAR_HOST_URL)/account/security/"
}

# --- Coverage + test report ------------------------------------------------
Info "Running pytest with coverage..."
Remove-Item -Force -ErrorAction SilentlyContinue junit.xml

# Python 3.14 + pytest-cov: gzip / sqlite finalizers raise ResourceWarning
# straight from the C-level unraisablehook, which BYPASSES pyproject.toml's
# filterwarnings (those only catch `warnings.warn()` calls). Setting
# PYTHONWARNINGS muzzles them at the interpreter level. Saved data is
# unaffected - this only silences the noise.
$prevPyWarnings = $env:PYTHONWARNINGS
$env:PYTHONWARNINGS = "ignore"

# Snapshot any pre-existing coverage.xml so we can fall back to it if pytest
# exits non-zero under PowerShell's NativeCommandError stderr-wrapping.
$coverageBackup = $null
if (Test-Path coverage.xml) {
    $coverageBackup = "coverage.xml.bak"
    Copy-Item -Force coverage.xml $coverageBackup
}
Remove-Item -Force -ErrorAction SilentlyContinue coverage.xml

# Run pytest inside a cmd /c subshell with stderr redirected to a temp file.
# Why both layers:
#   1. cmd /c isolates the process so PowerShell 5.1's NativeCommandError
#      doesn't promote pytest's ResourceWarning output to a terminating error.
#   2. 2>"$stderrFile" inside the cmd string makes cmd itself swallow the
#      stderr stream before it can reach PowerShell at all - even cmd's
#      output can otherwise leak back through PowerShell's host integration
#      and trip $ErrorActionPreference="Stop" on benign Python 3.14 warnings
#      (unraisable ResourceWarnings from sqlite/gzip finalizers, which are
#      cosmetic and don't affect saved data).
# Pytest's own test-progress output and failure summaries go to stdout, so
# we still see them. The stderr file only contains the noise; we dump its
# tail to the console only if pytest actually failed (exit != 0).
$stderrFile = Join-Path $Root "pytest-stderr.tmp"
Remove-Item -Force -ErrorAction SilentlyContinue $stderrFile
$pyArgs = "-m pytest --cov=app --cov-report=xml:coverage.xml --cov-report=term-missing:skip-covered --junitxml=junit.xml -q"

# Temporarily relax ErrorActionPreference so any residual stderr leakage
# doesn't turn into a terminating error and abort the whole script.
$prevErrAction = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & cmd /c "$python $pyArgs 2>`"$stderrFile`""
    $pytestExit = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevErrAction
}

$env:PYTHONWARNINGS = $prevPyWarnings

if ($pytestExit -ne 0 -and (Test-Path $stderrFile)) {
    Warn "pytest exited $pytestExit; tail of stderr:"
    Get-Content $stderrFile -Tail 20 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
}
Remove-Item -Force -ErrorAction SilentlyContinue $stderrFile

if ($pytestExit -ne 0 -or -not (Test-Path coverage.xml)) {
    Warn "pytest exit=$pytestExit. coverage.xml exists: $(Test-Path coverage.xml)"
    if ($coverageBackup -and (Test-Path $coverageBackup)) {
        Warn "Restoring previous coverage.xml so the scanner has data to ingest."
        Move-Item -Force $coverageBackup coverage.xml
    } else {
        Warn "No backup coverage.xml available - scan will report 0% coverage."
    }
} elseif ($coverageBackup) {
    # pytest succeeded and a backup exists from a previous run - tidy it up.
    # Guarded because Remove-Item's parameter binding errors on $null even
    # with -ErrorAction SilentlyContinue, which would halt the whole script.
    Remove-Item -Force -ErrorAction SilentlyContinue $coverageBackup
}

# --- Sonar scan via Docker -------------------------------------------------
Info "Running sonar-scanner-cli via Docker..."

# Docker Desktop on Windows has host.docker.internal built-in, which is the
# clean way for a container to reach the host. Rewrite localhost in
# SONAR_HOST_URL so the scanner-container can find SonarQube-container.
$effectiveHost = $env:SONAR_HOST_URL
if ($effectiveHost -match '://(localhost|127\.0\.0\.1)(:|/|$)') {
    $effectiveHost = $effectiveHost -replace '://(localhost|127\.0\.0\.1)', '://host.docker.internal'
    Info "Rewriting SONAR_HOST_URL for the scanner container: $effectiveHost"
}

$scannerArgs = @(
    "run", "--rm",
    "-e", "SONAR_HOST_URL=$effectiveHost"
)
if ($env:SONAR_TOKEN) {
    $scannerArgs += @("-e", "SONAR_TOKEN=$env:SONAR_TOKEN")
}
$scannerArgs += @(
    "-v", "$($Root):/usr/src",
    "sonarsource/sonar-scanner-cli:latest"
)

& docker @scannerArgs
if ($LASTEXITCODE -ne 0) { Abort "sonar-scanner failed (exit $LASTEXITCODE)" }

Info "Done. Browse results at $($env:SONAR_HOST_URL)/dashboard?id=Bug-Hunter-Enterprise"
