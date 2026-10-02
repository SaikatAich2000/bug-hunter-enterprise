# Windows version of scripts/sonar-scan.sh: run pytest with coverage, then push
# results to the local SonarQube via the sonar-scanner-cli Docker image.
# Usage: .\scripts\sonar-scan.ps1  (optionally set $env:SONAR_HOST_URL / $env:SONAR_TOKEN first)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info  { param([string]$msg) Write-Host "[SONAR] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Abort "Docker is not on PATH. Install Docker Desktop and re-open this shell."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Abort "Docker daemon is not running. Start Docker Desktop." }

$python = $null
foreach ($cmd in @("python", "py", "python3")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { $python = $cmd; break }
}
if (-not $python) { Abort "Python is not on PATH. Install Python 3.12 and re-open this shell." }

if (-not $env:SONAR_HOST_URL) { $env:SONAR_HOST_URL = "http://localhost:9090" }

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

Info "Running pytest with coverage..."
Remove-Item -Force -ErrorAction SilentlyContinue coverage.xml, junit.xml

# Python 3.14 + pytest-cov: C-level finalizer ResourceWarnings bypass pyproject
# filterwarnings, so silence them at the interpreter level. Coverage data unaffected.
$prevPyWarnings = $env:PYTHONWARNINGS
$env:PYTHONWARNINGS = "ignore"

try {
    & $python -m pytest `
        --cov=app `
        --cov-report=xml:coverage.xml `
        --cov-report=term-missing:skip-covered `
        --junitxml=junit.xml `
        -q
} catch {
    Warn "pytest exited non-zero - running scanner anyway so partial results land in SonarQube."
} finally {
    $env:PYTHONWARNINGS = $prevPyWarnings
}

Info "Running sonar-scanner-cli via Docker..."

# Rewrite localhost to host.docker.internal so the scanner container can reach SonarQube.
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
