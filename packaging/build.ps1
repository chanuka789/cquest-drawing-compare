<#
.SYNOPSIS
    Build the Windows application: frontend, then PyInstaller bundle.

.DESCRIPTION
    Runs the two steps in the only order that works — the frontend must be
    built before PyInstaller, because the spec bundles ui/dist as data.

    Run from anywhere:  .\packaging\build.ps1

.PARAMETER SkipUi
    Reuse the existing ui/dist instead of rebuilding the frontend.

.PARAMETER Clean
    Delete build/ and dist/ before building.
#>

[CmdletBinding()]
param(
    [switch]$SkipUi,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$AppName     = 'C-Quest Drawing Compare'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$UiDir       = Join-Path $ProjectRoot 'ui'
$UiDist      = Join-Path $UiDir 'dist'
$SpecFile    = Join-Path $PSScriptRoot 'build.spec'
$DistDir     = Join-Path $ProjectRoot 'dist'
$BuildDir    = Join-Path $ProjectRoot 'build'

function Write-Step([string]$Message) {
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

# npm and PyInstaller both write progress to stderr. Under
# $ErrorActionPreference = 'Stop', Windows PowerShell 5.1 turns that into a
# terminating NativeCommandError even when the tool succeeded, so native
# calls are judged by their exit code instead.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE."
    }
}

Push-Location $ProjectRoot
try {
    # ── Preconditions ──────────────────────────────────────────────────
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        throw "No virtual environment found at $python. Create it with 'python -m venv .venv'."
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw 'npm was not found on PATH. Install Node.js, then reopen PowerShell.'
    }

    if ($Clean) {
        Write-Step 'Removing previous build output'
        foreach ($dir in @($BuildDir, $DistDir)) {
            if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
        }
    }

    # ── 1. Frontend ────────────────────────────────────────────────────
    if ($SkipUi) {
        Write-Step 'Skipping the frontend build (-SkipUi)'
        if (-not (Test-Path (Join-Path $UiDist 'index.html'))) {
            throw "No built UI at $UiDist. Run without -SkipUi."
        }
    }
    else {
        Write-Step 'Building the frontend'
        Push-Location $UiDir
        try {
            if (-not (Test-Path 'node_modules')) {
                Write-Host 'node_modules is missing; running npm install first.'
                Invoke-Native 'npm install' { npm install }
            }
            Invoke-Native 'npm run build' { npm run build }
        }
        finally {
            Pop-Location
        }
    }

    # ── 2. PyInstaller ─────────────────────────────────────────────────
    Write-Step 'Building the Windows application'
    Invoke-Native 'PyInstaller' {
        & $python -m PyInstaller --noconfirm --distpath $DistDir --workpath $BuildDir $SpecFile
    }

    # ── 3. Report ──────────────────────────────────────────────────────
    $exePath = Join-Path $DistDir "$AppName\$AppName.exe"
    if (-not (Test-Path $exePath)) {
        throw "The build finished but $exePath does not exist."
    }

    $folder = Join-Path $DistDir $AppName
    $sizeMb = [math]::Round(
        ((Get-ChildItem $folder -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)

    Write-Step 'Build complete'
    Write-Host "  Application : $exePath"
    Write-Host "  Folder      : $folder"
    Write-Host "  Total size  : $sizeMb MB"
    Write-Host ''
    Write-Host '  Test it with CQDC_DEV unset, so it loads the built UI' -ForegroundColor Yellow
    Write-Host '  rather than the Vite dev server:' -ForegroundColor Yellow
    Write-Host "      `$env:CQDC_DEV = ''; & '$exePath'"
}
finally {
    Pop-Location
}
