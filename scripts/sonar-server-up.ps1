# Starts (or resumes) a local SonarQube Community server in Docker, for use
# with scripts/sonar-scan.ps1. One-time setup - after this, `docker start
# sonarqube` (or just re-running this script) brings it back up.
#
# Usage: .\scripts\sonar-server-up.ps1
$ErrorActionPreference = "Stop"

function Info  { param([string]$msg) Write-Host "[SONAR] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN] $msg"  -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR] $msg" -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Abort "Docker is not on PATH. Install Docker Desktop and re-open this shell."
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { Abort "Docker daemon is not running. Start Docker Desktop." }

$existing = docker ps -a --filter "name=^sonarqube$" --format "{{.Names}}"
if ($existing -eq "sonarqube") {
    Info "Container 'sonarqube' already exists - starting it."
    docker start sonarqube | Out-Null
} else {
    Info "Creating SonarQube Community container (first run downloads the image; this can take a few minutes)."
    # Community edition only, in-container H2 store (fine for a single local
    # dev instance - do not use this for a shared/production SonarQube).
    # Named volumes persist data/plugins/logs across restarts.
    docker volume create sonarqube_data | Out-Null
    docker volume create sonarqube_extensions | Out-Null
    docker volume create sonarqube_logs | Out-Null
    docker run -d --name sonarqube `
        -p 9090:9000 `
        -v sonarqube_data:/opt/sonarqube/data `
        -v sonarqube_extensions:/opt/sonarqube/extensions `
        -v sonarqube_logs:/opt/sonarqube/logs `
        sonarqube:community | Out-Null
}

Info "Waiting for SonarQube to report status UP (first boot can take 1-2 minutes)..."
$deadline = (Get-Date).AddMinutes(3)
$up = $false
while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:9090/api/system/status" -TimeoutSec 5
        if ($resp.status -eq "UP") { $up = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
}

if (-not $up) {
    Abort "SonarQube did not come up within 3 minutes. Check: docker logs sonarqube"
}

Info "SonarQube is UP at http://localhost:9090"
Info "First time only: log in with admin/admin at that URL, set a new password,"
Info "then generate a token at http://localhost:9090/account/security/ and:"
Info '  $env:SONAR_TOKEN = "<paste token>"'
Info "Then run: .\scripts\sonar-scan.ps1"
