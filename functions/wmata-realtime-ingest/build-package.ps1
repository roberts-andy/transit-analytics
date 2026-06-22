<#
.SYNOPSIS
    Builds a deployment-ready zip package for the WMATA real-time ingest Azure Function.

.DESCRIPTION
    Creates an isolated Python 3.11 virtual environment, installs dependencies,
    and produces a self-contained zip with .python_packages laid out the way
    the Azure Functions Python worker expects. The resulting zip can be uploaded
    to blob storage and referenced via WEBSITE_RUN_FROM_PACKAGE.

.PARAMETER OutputPath
    Where to write the zip file. Defaults to .\dist\wmata-func.zip

.PARAMETER PythonPath
    Path to the Python 3.11 executable. The script will auto-detect common
    install locations if not provided.

.EXAMPLE
    .\build-package.ps1
    .\build-package.ps1 -OutputPath C:\deploy\wmata.zip
    .\build-package.ps1 -PythonPath "C:\Python311\python.exe"
#>

[CmdletBinding()]
param(
    [string]$OutputPath,
    [string]$PythonPath
)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot

# --- Resolve output path ---
if (-not $OutputPath) {
    $OutputPath = Join-Path $scriptDir "dist\wmata-func.zip"
}
$outputDir = Split-Path $OutputPath -Parent
if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}

# --- Locate Python 3.11 ---
function Find-Python311 {
    # Check explicit param first
    if ($PythonPath -and (Test-Path $PythonPath)) {
        return $PythonPath
    }

    # Common install locations on Windows
    $candidates = @(
        "py -3.11"                                                          # py launcher
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"            # user install
        "C:\Python311\python.exe"                                           # custom install
        "$env:ProgramFiles\Python311\python.exe"                            # system install
        "$env:ProgramFiles(x86)\Python311\python.exe"
    )

    # Try the py launcher first (most reliable on Windows)
    try {
        $ver = & py -3.11 --version 2>&1
        if ($ver -match "Python 3\.11") {
            return "py -3.11"
        }
    } catch { }

    # Fall back to known paths
    foreach ($c in $candidates) {
        if (Test-Path $c) {
            $ver = & $c --version 2>&1
            if ($ver -match "Python 3\.11") {
                return $c
            }
        }
    }

    return $null
}

$python = Find-Python311
if (-not $python) {
    Write-Error @"
Python 3.11 not found. The Azure Function App is configured for Python 3.11.

Install it from https://www.python.org/downloads/release/python-3119/
or via:  winget install Python.Python.3.11

Then re-run this script, or pass -PythonPath explicitly.
"@
    exit 1
}

Write-Host "Using Python: $python" -ForegroundColor Cyan

# --- Reuse persistent .venv (create only if missing) ---
$venvDir  = Join-Path $scriptDir ".venv"
$stageDir = Join-Path ([System.IO.Path]::GetTempPath()) "wmata-func-stage"

if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory -Path $stageDir -Force | Out-Null

# Create venv only if it doesn't exist or Python version changed
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$needsVenv  = -not (Test-Path $venvPython)

if (-not $needsVenv) {
    $existingVer = & $venvPython --version 2>&1
    if ($existingVer -notmatch "Python 3\.11") {
        Write-Host "Existing venv is $existingVer — recreating for 3.11..." -ForegroundColor DarkYellow
        Remove-Item -Recurse -Force $venvDir
        $needsVenv = $true
    }
}

if ($needsVenv) {
    Write-Host "Creating Python 3.11 virtual environment..." -ForegroundColor Cyan
    if ($python -eq "py -3.11") {
        & py -3.11 -m venv $venvDir
    } else {
        & $python -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }

    # Upgrade pip in new venv
    & $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null
} else {
    Write-Host "Reusing existing .venv" -ForegroundColor Cyan
}

$venvPip = Join-Path $venvDir "Scripts\pip.exe"

# Verify version
$actualVer = & $venvPython --version 2>&1
Write-Host "Virtual environment Python: $actualVer" -ForegroundColor Green

try {
    # Install dependencies into the .python_packages layout
    $packagesDir = Join-Path $stageDir ".python_packages\lib\site-packages"
    Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Cyan
    & $venvPip install `
        -r (Join-Path $scriptDir "requirements.txt") `
        --target $packagesDir `
        --quiet `
        --upgrade
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

    # Copy function source files
    $sourceFiles = @("function_app.py", "host.json", "requirements.txt")
    foreach ($f in $sourceFiles) {
        $src = Join-Path $scriptDir $f
        if (Test-Path $src) {
            Copy-Item $src -Destination $stageDir
            Write-Host "  + $f" -ForegroundColor DarkGray
        }
    }

    # Include function.json if present
    if (Test-Path (Join-Path $scriptDir "function.json")) {
        Copy-Item (Join-Path $scriptDir "function.json") -Destination $stageDir
        Write-Host "  + function.json" -ForegroundColor DarkGray
    }

    # Build the zip
    if (Test-Path $OutputPath) { Remove-Item $OutputPath -Force }
    Write-Host "Creating zip package..." -ForegroundColor Cyan
    Compress-Archive -Path "$stageDir\*" -DestinationPath $OutputPath

    $zipSize = (Get-Item $OutputPath).Length
    $sizeMB  = [math]::Round($zipSize / 1MB, 2)

    Write-Host ""
    Write-Host "Build complete!" -ForegroundColor Green
    Write-Host "  Package: $OutputPath" -ForegroundColor Green
    Write-Host "  Size:    $sizeMB MB" -ForegroundColor Green
    Write-Host ""
    Write-Host "To deploy, run:" -ForegroundColor Yellow
    Write-Host "  .\infra\deploy-wmata.ps1 -SkipInfraDeploy" -ForegroundColor DarkYellow
}
finally {
    # Clean up staging directory (venv is preserved for next build)
    if (Test-Path $stageDir) {
        Remove-Item -Recurse -Force $stageDir -ErrorAction SilentlyContinue
    }
}
