$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

Set-Location $Root
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run .\scripts\install.ps1 first."
}

$env:SCRAPEX_ROOT = $Root
# Capture the exit code before any pipeline: an early-terminating pipeline
# element such as `Select-Object -First 1` leaves $LASTEXITCODE unset, which
# silently blanked the revision and made every caller treat this runtime as
# stale source.
$RevisionOutput = & git -C $Root rev-parse HEAD 2>$null
$RevisionExitCode = $LASTEXITCODE
$Revision = ([string]($RevisionOutput | Select-Object -First 1)).Trim()
if ($RevisionExitCode -eq 0 -and $Revision -match '^[0-9a-fA-F]{40}$') {
    $env:SCRAPEX_RUNTIME_REVISION = $Revision
} else {
    $env:SCRAPEX_RUNTIME_REVISION = ""
}
$EndpointJson = & $Python -c "import json; from scrapex.config import Settings; settings = Settings.load(); print(json.dumps({'host': settings.host, 'port': settings.port}))"
if ($LASTEXITCODE -ne 0) {
    throw "ScrapeX configuration preflight failed."
}
$Endpoint = $EndpointJson | ConvertFrom-Json
$BindHost = [string]$Endpoint.host
$Port = [int]$Endpoint.port
$env:SCRAPEX_HOST = $BindHost
$env:SCRAPEX_PORT = [string]$Port

$DisplayHost = if ($BindHost.Contains(":")) { "[$BindHost]" } else { $BindHost }
Write-Host "ScrapeX -> http://${DisplayHost}:$Port" -ForegroundColor Cyan
& $Python -m scrapex
