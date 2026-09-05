param(
    [switch]$CreatePrivateRemote,
    [string]$RepositoryName = "ScrapeX",
    [string]$RemoteName = "origin"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not on PATH."
}

function Invoke-CheckedGit {
    param([string[]]$Arguments)

    & git @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $Root ".git"))) {
    Invoke-CheckedGit -Arguments @("init", "-b", "main")
    Write-Host "Initialized an empty ScrapeX repository on main." -ForegroundColor Green
}
else {
    $ResolvedRoot = (& git rev-parse --show-toplevel).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "The existing .git directory is not a usable repository."
    }
    if ([System.IO.Path]::GetFullPath($ResolvedRoot) -ne [System.IO.Path]::GetFullPath($Root)) {
        throw "The existing Git repository does not resolve to the ScrapeX root."
    }
    Write-Host "Reusing the existing ScrapeX repository." -ForegroundColor Green
}

Write-Host "`nCurrent Git state:" -ForegroundColor Cyan
Invoke-CheckedGit -Arguments @("status", "--short", "--branch")

Write-Host "`nReview-only staging preview (the index will not be changed):" -ForegroundColor Cyan
Invoke-CheckedGit -Arguments @("add", "--dry-run", "--all")

if ($CreatePrivateRemote) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI is required to create a private remote."
    }
    & git rev-parse --verify HEAD *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Review, stage, and create the initial local commit before creating a remote."
    }
    & git remote get-url $RemoteName *> $null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Remote '$RemoteName' already exists; nothing was changed." -ForegroundColor Yellow
    }
    else {
        & gh repo create $RepositoryName --private --source $Root --remote $RemoteName
        if ($LASTEXITCODE -ne 0) {
            throw "Private GitHub repository creation failed."
        }
        Write-Host "Created private remote '$RemoteName'. No push was performed." -ForegroundColor Green
    }
}

Write-Host @"

No files were staged, committed, or pushed.
Review the preview above, then stage explicit reviewed paths and commit on main.
Create the private remote later with:
  .\scripts\init-github.ps1 -CreatePrivateRemote
Push only after verifying the local commit and destination account.
"@ -ForegroundColor Yellow
