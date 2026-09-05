$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

Set-Location $Root
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run .\scripts\install.ps1 first."
}

$env:SCRAPEX_ROOT = $Root
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
