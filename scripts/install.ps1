$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Constraints = Join-Path $Root "constraints.txt"
$VersionCheck = @"
import sys
raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) else 1)
"@

Set-Location $Root
Write-Host "`n=== ScrapeX standalone install / repair ===" -ForegroundColor Cyan

function Test-SupportedPython {
    param(
        [string]$File,
        [string[]]$Arguments = @()
    )

    try {
        & $File @Arguments -c $VersionCheck *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

if (Test-Path -LiteralPath $VenvPython) {
    if (-not (Test-SupportedPython -File $VenvPython)) {
        throw "The existing ScrapeX virtual environment must use Python 3.11 through 3.14."
    }
}
else {
    $Launchers = @(
        [pscustomobject]@{ File = "py"; Arguments = @("-3") },
        [pscustomobject]@{ File = "python"; Arguments = @() },
        [pscustomobject]@{ File = "python3"; Arguments = @() }
    )
    $Selected = $null
    foreach ($Launcher in $Launchers) {
        if (
            (Get-Command $Launcher.File -ErrorAction SilentlyContinue) -and
            (Test-SupportedPython -File $Launcher.File -Arguments $Launcher.Arguments)
        ) {
            $Selected = $Launcher
            break
        }
    }
    if ($null -eq $Selected) {
        throw "Install a standalone Python 3.11 through 3.14 interpreter for ScrapeX."
    }

    $LauncherArguments = @($Selected.Arguments)
    & $Selected.File @LauncherArguments -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        throw "Could not create the ScrapeX virtual environment."
    }
}

if (-not (Test-Path -LiteralPath $Constraints)) {
    throw "ScrapeX dependency constraints are missing: $Constraints"
}

& $VenvPython --version | Out-Host
& $VenvPython -m pip install --disable-pip-version-check --constraint $Constraints -e ".[dev]"
if ($LASTEXITCODE -ne 0) {
    throw "ScrapeX dependency install failed."
}
& $VenvPython -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "ScrapeX dependency verification failed."
}

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null

Write-Host "`n=== ScrapeX tests ===" -ForegroundColor Cyan
$env:PYTHONDONTWRITEBYTECODE = "1"
& $VenvPython -m pytest -p no:cacheprovider -q
if ($LASTEXITCODE -ne 0) {
    throw "ScrapeX tests failed."
}

Write-Host "`nScrapeX ready. Start with .\scripts\start.ps1" -ForegroundColor Green
Write-Host "ALLDATA automation is frozen/manual-future; no browser was installed or launched." -ForegroundColor Yellow
