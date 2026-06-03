# =============================================================================
#  scripts/sonar-export.ps1
# -----------------------------------------------------------------------------
#  Pull every Issue and every Security Hotspot from the locally-running
#  SonarQube into local files so they can be reviewed / triaged / fed to
#  an LLM / diffed across scans.
#
#  Usage:
#      $env:SONAR_TOKEN = "sqp_xxxxxxxxxxxx"
#      .\scripts\sonar-export.ps1
#
#  Outputs (gitignored):
#      sonar-issues.json     - raw paginated /api/issues/search dump
#      sonar-hotspots.json   - raw paginated /api/hotspots/search dump
#      sonar-issues.csv      - flat CSV: severity, type, file, line, message, rule
#      sonar-hotspots.csv    - flat CSV: vulnerabilityProbability, status, file, line, message
#
#  Database safety: read-only API calls. Writes only to the four files
#  listed above (all gitignored). No DB access of any kind.
#
#  ASCII-only on purpose. Windows PowerShell 5.1 reads .ps1 files as the
#  console code page (Windows-1252) when no BOM is present, so any UTF-8
#  multi-byte character (em-dash, ellipsis, arrow) corrupts string parsing.
# =============================================================================
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info  { param([string]$msg) Write-Host "[EXPORT] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN]   $msg" -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR]  $msg" -ForegroundColor Red; exit 1 }

# --- Config ----------------------------------------------------------------
if (-not $env:SONAR_HOST_URL) { $env:SONAR_HOST_URL = "http://localhost:9000" }
if (-not $env:SONAR_TOKEN) {
    Abort "SONAR_TOKEN is not set. Run: `$env:SONAR_TOKEN = 'sqp_xxxx' first."
}
$ProjectKey = "Bug-Hunter-Enterprise"
$Base = $env:SONAR_HOST_URL.TrimEnd("/")

# Sonar accepts the token as the Basic-auth USERNAME with an empty password.
$pair = "$($env:SONAR_TOKEN):"
$bytes = [System.Text.Encoding]::ASCII.GetBytes($pair)
$AuthHeader = @{ Authorization = "Basic " + [Convert]::ToBase64String($bytes) }

# --- Pre-flight ------------------------------------------------------------
try {
    $status = Invoke-RestMethod -Uri "$Base/api/system/status" -TimeoutSec 5 -Headers $AuthHeader
    if ($status.status -ne "UP") { Abort "SonarQube isn't UP (status: $($status.status))" }
} catch {
    Abort "Can't reach SonarQube at $Base. Is the container running? ($($_.Exception.Message))"
}
Info "SonarQube reachable at $Base"

# --- Paginated fetch helper -----------------------------------------------
function Get-AllPages {
    param(
        [string]$Path,
        [hashtable]$Query,
        [string]$ItemsKey
    )
    $all = @()
    $page = 1
    $pageSize = 500
    while ($true) {
        $qs = ($Query.GetEnumerator() | ForEach-Object { "$($_.Key)=$([uri]::EscapeDataString($_.Value))" }) -join "&"
        $url = "$Base$Path`?$qs&ps=$pageSize&p=$page"
        $resp = Invoke-RestMethod -Uri $url -Headers $AuthHeader
        $items = $resp.$ItemsKey
        if ($null -eq $items -or $items.Count -eq 0) { break }
        $all += $items

        $total = $resp.paging.total
        if ($null -eq $total) { $total = $resp.total }
        if ($all.Count -ge $total) { break }

        if ($page * $pageSize -ge 10000) {
            Warn "Hit Sonar's 10k pagination ceiling at page $page; not paging further."
            break
        }
        $page++
    }
    return $all
}

# --- Issues ----------------------------------------------------------------
Info "Fetching issues..."
$issues = Get-AllPages -Path "/api/issues/search" -Query @{
    componentKeys = $ProjectKey
    resolved      = "false"
    s             = "FILE_LINE"
    asc           = "true"
} -ItemsKey "issues"
Info "Got $($issues.Count) open issues"

$issues | ConvertTo-Json -Depth 12 | Out-File -Encoding utf8 sonar-issues.json
Info "Wrote sonar-issues.json"

$issues | ForEach-Object {
    $comp = $_.component
    $file = if ($comp -like "$ProjectKey`:*") { $comp.Substring($ProjectKey.Length + 1) } else { $comp }
    [PSCustomObject]@{
        severity = $_.severity
        type     = $_.type
        rule     = $_.rule
        file     = $file
        line     = $_.line
        message  = ($_.message -replace "[\r\n]+", " ")
        effort   = $_.effort
        tags     = ($_.tags -join ";")
    }
} | Export-Csv -NoTypeInformation -Path sonar-issues.csv -Encoding UTF8
Info "Wrote sonar-issues.csv"

# --- Hotspots --------------------------------------------------------------
# Hotspots live on a separate API endpoint. They are NOT "issues" in
# Sonar's data model; they are a review-required category.
#
# Permission gotcha: Project Analysis Tokens (sqp_*) can UPLOAD scans
# but they CANNOT read hotspots back. To export hotspots too, generate
# a USER token (sqa_*) at $SONAR_HOST_URL/account/security/ (type
# "User Token"). We catch the 403 so a project-analysis token still
# produces sonar-issues.* successfully.
Info "Fetching security hotspots..."
$hotspots = @()
try {
    $hotspots = Get-AllPages -Path "/api/hotspots/search" -Query @{
        projectKey = $ProjectKey
        status     = "TO_REVIEW"
    } -ItemsKey "hotspots"
    Info "Got $($hotspots.Count) TO_REVIEW hotspots"
} catch {
    $msg = $_.Exception.Message
    if ($msg -match "403|Forbidden") {
        Warn "Hotspots endpoint returned 403 - your token can analyse but cannot"
        Warn "read hotspots back. To export hotspots too, generate a USER token at:"
        Warn ("  " + $Base + "/account/security/")
        Warn "Set SONAR_TOKEN to the new token and re-run this script."
        Warn "Continuing without hotspots - sonar-issues.* still written."
    } else {
        Warn ("Hotspots fetch failed: " + $msg)
        Warn "Continuing without hotspots - sonar-issues.* still written."
    }
}

if ($hotspots.Count -gt 0) {
    $hotspots | ConvertTo-Json -Depth 12 | Out-File -Encoding utf8 sonar-hotspots.json
    Info "Wrote sonar-hotspots.json"

    $hotspots | ForEach-Object {
        $comp = $_.component
        $file = if ($comp -like "$ProjectKey`:*") { $comp.Substring($ProjectKey.Length + 1) } else { $comp }
        [PSCustomObject]@{
            vulnProb = $_.vulnerabilityProbability
            status   = $_.status
            category = $_.securityCategory
            rule     = $_.ruleKey
            file     = $file
            line     = $_.line
            message  = ($_.message -replace "[\r\n]+", " ")
        }
    } | Export-Csv -NoTypeInformation -Path sonar-hotspots.csv -Encoding UTF8
    Info "Wrote sonar-hotspots.csv"
}

# --- Summary --------------------------------------------------------------
Write-Host ""
Info "==========================  Summary  =========================="
Write-Host ("  Open issues by type:") -ForegroundColor Cyan
$issues | Group-Object type | Sort-Object Count -Descending | ForEach-Object {
    "    {0,-15} {1}" -f $_.Name, $_.Count
}
Write-Host ("  Open issues by severity:") -ForegroundColor Cyan
$issues | Group-Object severity | Sort-Object Count -Descending | ForEach-Object {
    "    {0,-15} {1}" -f $_.Name, $_.Count
}
Write-Host ("  Open issues by file (top 10):") -ForegroundColor Cyan
$issues | Group-Object {
    $c = $_.component
    if ($c -like "$ProjectKey`:*") { $c.Substring($ProjectKey.Length + 1) } else { $c }
} | Sort-Object Count -Descending | Select-Object -First 10 | ForEach-Object {
    "    {0,-50} {1}" -f $_.Name, $_.Count
}
Write-Host ("  Open issues by rule (top 15):") -ForegroundColor Cyan
$issues | Group-Object rule | Sort-Object Count -Descending | Select-Object -First 15 | ForEach-Object {
    "    {0,-25} {1}" -f $_.Name, $_.Count
}
Write-Host ("  Security hotspots by vulnerability probability:") -ForegroundColor Cyan
$hotspots | Group-Object vulnerabilityProbability | Sort-Object Count -Descending | ForEach-Object {
    "    {0,-15} {1}" -f $_.Name, $_.Count
}
Write-Host ""
Info "Done. Files written to repo root: sonar-issues.{json,csv} sonar-hotspots.{json,csv}"
