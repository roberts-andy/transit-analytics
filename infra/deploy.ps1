<#
.SYNOPSIS
    Deploys core Azure infrastructure for transit-analytics.

.DESCRIPTION
    Runs the main Bicep template to deploy:
    - Resource Group
    - Key Vault (with RBAC)
    - WMATA Function App + Event Hub (if module present)

.EXAMPLE
    .\infra\deploy.ps1
#>

$ErrorActionPreference = "Stop"

Write-Host "Deploying transit-analytics infrastructure..." -ForegroundColor Yellow

az deployment sub create `
    --location eastus `
    --template-file (Join-Path $PSScriptRoot "main.bicep") `
    --parameters (Join-Path $PSScriptRoot "main.bicepparam")

if ($LASTEXITCODE -ne 0) {
    Write-Error "Deployment failed."
    exit 1
}

Write-Host "✓ Deployment complete." -ForegroundColor Green
