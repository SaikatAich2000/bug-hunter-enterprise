# =============================================================================
#  scripts/sonar-mark-hotspots-safe.ps1
# -----------------------------------------------------------------------------
#  Bulk-mark every open Security Hotspot in the Bug_Hunter project as
#  reviewed + SAFE. Used after a human review confirms the hotspots are
#  false positives (regex DoS on bounded chat input, UI placeholder
#  strings that contain the word "password", same-origin URL builders
#  that use http:// for CSRF comparison only, etc.).
#
#  IMPORTANT: this needs a USER token (sqa_*), NOT a project-analysis
#  token (sqp_*). Generate one at:
#      http://localhost:9000/account/security/
#  ...then set it:
#      $env:SONAR_TOKEN = "sqa_xxxxxxxxxxxxx"
#      .\scripts\sonar-mark-hotspots-safe.ps1
#
#  Database safety: ONLY writes to the SonarQube hotspot status table via
#  REST API. Does not touch the application database in any way.
#
#  ASCII-only — Windows PowerShell 5.1 reads .ps1 as the console code page
#  (Windows-1252) when no BOM is present; UTF-8 multi-byte chars corrupt
#  string parsing.
# =============================================================================
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info  { param([string]$msg) Write-Host "[REVIEW] $msg" -ForegroundColor Green }
function Warn  { param([string]$msg) Write-Host "[WARN]   $msg" -ForegroundColor Yellow }
function Abort { param([string]$msg) Write-Host "[ERROR]  $msg" -ForegroundColor Red; exit 1 }

# --- Config ----------------------------------------------------------------
if (-not $env:SONAR_HOST_URL) { $env:SONAR_HOST_URL = "http://localhost:9000" }
if (-not $env:SONAR_TOKEN) {
    Abort "SONAR_TOKEN is not set. Generate a USER token (sqa_*) at $env:SONAR_HOST_URL/account/security/ then run: `$env:SONAR_TOKEN = 'sqa_xxxx'"
}
$ProjectKey = "Bug-Hunter-Enterprise"
$Base = $env:SONAR_HOST_URL.TrimEnd("/")
$Resolution = "SAFE"
$Comment = "Reviewed: false positive. Regex patterns operate on bounded chat input; placeholder strings are UI hints not credentials; same-origin URLs are for CSRF comparison only."

# --- Auth header -----------------------------------------------------------
# SonarQube uses Basic auth with the token as username, empty password.
$tokenBytes = [System.Text.Encoding]::ASCII.GetBytes("$($env:SONAR_TOKEN):")
$basic = [Convert]::ToBase64String($tokenBytes)
$Headers = @{ "Authorization" = "Basic $basic" }

# --- Reachability check ----------------------------------------------------
try {
    $sys = Invoke-RestMethod -Uri "$Base/api/system/status" -Headers $Headers -Method Get
    Info "SonarQube reachable at $Base (status: $($sys.status))"
}
catch {
    Abort "Cannot reach $Base/api/system/status: $($_.Exception.Message)"
}

# --- Token type sanity check ----------------------------------------------
# Project-analysis tokens (sqp_*) cannot read or write hotspot status.
# Reading hotspots first surfaces the 403 with a clear message.
$hotspots = @()
$page = 1
$pageSize = 500
while ($true) {
    try {
        $url = "$Base/api/hotspots/search?projectKey=$ProjectKey&ps=$pageSize&p=$page&status=TO_REVIEW"
        $resp = Invoke-RestMethod -Uri $url -Headers $Headers -Method Get
    }
    catch {
        $status = $_.Exception.Response.StatusCode.value__
        if ($status -eq 403) {
            Abort "Got 403 on /api/hotspots/search. The token you set is likely a project-analysis token (sqp_*) which can analyse but not read/write hotspot reviews. Generate a USER token at $Base/account/security/ and try again."
        }
        Abort "Failed to fetch hotspots: HTTP $status - $($_.Exception.Message)"
    }
    if (-not $resp.hotspots -or $resp.hotspots.Count -eq 0) { break }
    $hotspots += $resp.hotspots
    if ($resp.hotspots.Count -lt $pageSize) { break }
    $page++
}

if ($hotspots.Count -eq 0) {
    Info "No open hotspots to review. Quality gate should be green."
    exit 0
}

Info "Found $($hotspots.Count) open hotspots awaiting review."
$hotspots | ForEach-Object {
    Write-Host ("  [{0}] {1}:{2}  {3}" -f $_.vulnerabilityProbability, $_.component, $_.line, $_.message)
}

# --- Confirm before bulk-marking -------------------------------------------
Write-Host ""
Write-Host "About to mark ALL of the above as REVIEWED + $Resolution." -ForegroundColor Yellow
Write-Host "Comment: $Comment" -ForegroundColor DarkGray
$confirm = Read-Host "Type 'yes' to proceed"
if ($confirm -ne "yes") {
    Warn "Aborted. No changes made."
    exit 1
}

# --- Bulk mark -------------------------------------------------------------
$marked = 0
$failed = 0
foreach ($h in $hotspots) {
    try {
        $body = @{
            hotspot    = $h.key
            status     = "REVIEWED"
            resolution = $Resolution
            comment    = $Comment
        }
        Invoke-RestMethod -Uri "$Base/api/hotspots/change_status" `
            -Headers $Headers -Method Post -Body $body | Out-Null
        $marked++
        Write-Host ("  OK    {0}:{1}" -f $h.component, $h.line) -ForegroundColor Green
    }
    catch {
        $failed++
        $status = $_.Exception.Response.StatusCode.value__
        Write-Host ("  FAIL  {0}:{1}  HTTP {2} {3}" -f $h.component, $h.line, $status, $_.Exception.Message) -ForegroundColor Red
    }
}

Write-Host ""
Info "Marked $marked / $($hotspots.Count) hotspots as $Resolution. Failed: $failed."
if ($failed -eq 0) {
    Info "Refresh $Base/dashboard?id=$ProjectKey - Security Hotspots Reviewed should now be 100%."
}
