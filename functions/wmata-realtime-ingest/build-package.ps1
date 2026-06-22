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

# --- Set up temp build directory ---
$buildRoot = Join-Path ([System.IO.Path]::GetTempPath()) "wmata-func-build-$(Get-Random)"
$venvDir   = Join-Path $buildRoot "venv"
$stageDir  = Join-Path $buildRoot "stage"

try {
    New-Item -ItemType Directory -Path $buildRoot, $stageDir -Force | Out-Null

    # Create virtual environment
    Write-Host "Creating Python 3.11 virtual environment..." -ForegroundColor Cyan
    if ($python -eq "py -3.11") {
        & py -3.11 -m venv $venvDir
    } else {
        & $python -m venv $venvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to create virtual environment" }

    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    $venvPip    = Join-Path $venvDir "Scripts\pip.exe"

    # Verify version
    $actualVer = & $venvPython --version 2>&1
    Write-Host "Virtual environment Python: $actualVer" -ForegroundColor Green

    # Upgrade pip
    & $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null

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
    Write-Host "To deploy:" -ForegroundColor Yellow
    Write-Host "  1. Upload:  az storage blob upload --account-name <storage> --container-name deployment --file `"$OutputPath`" --name wmata-func.zip --auth-mode login --overwrite" -ForegroundColor DarkYellow
    Write-Host "  2. Set app: az functionapp config appsettings set -n wmata-ingest-func -g transit-analytics-rg --settings WEBSITE_RUN_FROM_PACKAGE=`"https://<storage>.blob.core.windows.net/deployment/wmata-func.zip`"" -ForegroundColor DarkYellow
    Write-Host "  3. Restart: az functionapp restart -n wmata-ingest-func -g transit-analytics-rg" -ForegroundColor DarkYellow
}
finally {
    # Clean up temp build directory
    if (Test-Path $buildRoot) {
        Remove-Item -Recurse -Force $buildRoot -ErrorAction SilentlyContinue
    }
}
