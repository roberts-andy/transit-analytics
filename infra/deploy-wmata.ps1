<#
.SYNOPSIS
    Deploys WMATA real-time ingestion infrastructure (Function App + Event Hub)
    and publishes the Azure Function code.

.DESCRIPTION
    1. Deploys the Bicep template (Key Vault + WMATA Function App + Event Hub)
    2. Stores the WMATA API key in Key Vault
    3. Grants the Function App managed identity Key Vault Secrets User access
    4. Publishes the Function App code

.PARAMETER WmataApiKey
    Your WMATA developer API key. Will be stored in Key Vault.

.PARAMETER SkipInfraDeploy
    Skip Bicep deployment (use if infra already exists).

.PARAMETER SkipFunctionDeploy
    Skip Function App code publish (use if only updating infra).

.EXAMPLE
    .\infra\deploy-wmata.ps1 -WmataApiKey "your-key-here"
#>

param(
    [Parameter(Mandatory = $false)]
    [string]$WmataApiKey,

    [switch]$SkipInfraDeploy,
    [switch]$SkipFunctionDeploy
)

$ErrorActionPreference = "Stop"

# Configuration
$Location = "eastus"
$ResourceGroup = "transit-analytics-rg"
$KeyVaultName = "kvtransitdemo-f70cfb6a"
$FunctionAppName = "wmata-ingest-func"
$FunctionProjectPath = Join-Path $PSScriptRoot "..\functions\wmata-realtime-ingest"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host " WMATA Real-Time Ingestion — Deployment" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Resource Group:   $ResourceGroup"
Write-Host "  Location:         $Location"
Write-Host "  Key Vault:        $KeyVaultName"
Write-Host "  Function App:     $FunctionAppName"
Write-Host ""

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Deploy Bicep infrastructure
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipInfraDeploy) {
    Write-Host "[1/4] Deploying infrastructure (Bicep)..." -ForegroundColor Yellow

    $deployResult = az deployment sub create `
        --location $Location `
        --template-file (Join-Path $PSScriptRoot "main.bicep") `
        --parameters (Join-Path $PSScriptRoot "main.bicepparam") `
        --output json | ConvertFrom-Json

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Bicep deployment failed."
        exit 1
    }

    $functionPrincipalId = $deployResult.properties.outputs.wmataFunctionAppName.value
    Write-Host "  ✓ Infrastructure deployed" -ForegroundColor Green
    Write-Host "    Function App: $($deployResult.properties.outputs.wmataFunctionAppName.value)"
    Write-Host "    Event Hub:    $($deployResult.properties.outputs.wmataEventHubNamespace.value)/$($deployResult.properties.outputs.wmataEventHubName.value)"
}
else {
    Write-Host "[1/4] Skipping infrastructure deployment (--SkipInfraDeploy)" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Store WMATA API key in Key Vault
# ─────────────────────────────────────────────────────────────────────────────
if ($WmataApiKey) {
    Write-Host "[2/4] Storing WMATA API key in Key Vault..." -ForegroundColor Yellow

    az keyvault secret set `
        --vault-name $KeyVaultName `
        --name "wmata-api-key" `
        --value $WmataApiKey `
        --output none

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to store secret. Ensure you have Key Vault access and public network is enabled."
        exit 1
    }

    Write-Host "  ✓ Secret 'wmata-api-key' stored in '$KeyVaultName'" -ForegroundColor Green
}
else {
    Write-Host "[2/4] Skipping secret storage (no -WmataApiKey provided)" -ForegroundColor DarkGray
    Write-Host "       Run later: az keyvault secret set --vault-name $KeyVaultName --name wmata-api-key --value <key>"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Grant Function App managed identity Key Vault access
# ─────────────────────────────────────────────────────────────────────────────
Write-Host "[3/4] Granting Function App Key Vault access..." -ForegroundColor Yellow

$funcIdentity = az functionapp identity show `
    --name $FunctionAppName `
    --resource-group $ResourceGroup `
    --query principalId -o tsv 2>$null

if ($funcIdentity) {
    $kvResourceId = az keyvault show `
        --name $KeyVaultName `
        --resource-group $ResourceGroup `
        --query id -o tsv

    az role assignment create `
        --assignee $funcIdentity `
        --role "Key Vault Secrets User" `
        --scope $kvResourceId `
        --output none 2>$null

    Write-Host "  ✓ Key Vault Secrets User granted to $funcIdentity" -ForegroundColor Green
}
else {
    Write-Host "  ⚠ Function App not found or no identity — grant manually after deploy" -ForegroundColor DarkYellow
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Publish Function App code
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipFunctionDeploy) {
    Write-Host "[4/4] Publishing Function App code..." -ForegroundColor Yellow

    if (-not (Get-Command func -ErrorAction SilentlyContinue)) {
        Write-Error "Azure Functions Core Tools (func) not found. Install: npm install -g azure-functions-core-tools@4"
        exit 1
    }

    Push-Location $FunctionProjectPath
    try {
        # Install Python dependencies
        if (Test-Path "requirements.txt") {
            Write-Host "  Installing Python dependencies..."
            pip install -r requirements.txt --target .python_packages/lib/site-packages --quiet
        }

        # Publish to Azure
        Write-Host "  Publishing to $FunctionAppName..."
        func azure functionapp publish $FunctionAppName --python

        if ($LASTEXITCODE -ne 0) {
            Write-Error "Function App publish failed."
            exit 1
        }

        Write-Host "  ✓ Function App published" -ForegroundColor Green
    }
    finally {
        Pop-Location
    }
}
else {
    Write-Host "[4/4] Skipping Function App deploy (--SkipFunctionDeploy)" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host " Deployment Complete" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. In Fabric, create an Eventstream connected to:"
Write-Host "       Event Hub:       wmata-ingest-ehns / wmata-realtime"
Write-Host "       Consumer Group:  fabric-eventstream"
Write-Host "       Auth Rule:       fabric-listen"
Write-Host ""
Write-Host "    2. Route Eventstream output to:"
Write-Host "       Lakehouse:  bronze"
Write-Host "       Schema:     wmata"
Write-Host "       Table:      realtime_events"
Write-Host ""
Write-Host "    3. Verify Function is running:"
Write-Host "       az functionapp show --name $FunctionAppName --resource-group $ResourceGroup --query state -o tsv"
Write-Host ""
