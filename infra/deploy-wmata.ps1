<#
.SYNOPSIS
    Deploys WMATA real-time ingestion infrastructure and Function App code.

.DESCRIPTION
    1. Deploys the Bicep template (Key Vault ref + WMATA Function App + Event Hub)
    2. Stores the WMATA API key in Key Vault (optional — skip if already set)
    3. Grants the Function App managed identity Key Vault Secrets User access
    4. Builds a self-contained Python zip via build-package.ps1 (requires Python 3.11)
    5. Grants deployer Storage Blob Data Owner for identity-based upload
    6. Uploads zip to blob storage and sets WEBSITE_RUN_FROM_PACKAGE

    This avoids the func CLI and shared-key auth, which are blocked by
    subscription Azure Policy on the storage account.

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
$StorageAccountName = "wmataingeststor"
$DeployContainer = "deploymentpackage"
$BlobName = "wmata-func.zip"
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
    Write-Host "[1/5] Deploying infrastructure (Bicep)..." -ForegroundColor Yellow

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
    Write-Host "[1/5] Skipping infrastructure deployment (--SkipInfraDeploy)" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Store WMATA API key in Key Vault
# ─────────────────────────────────────────────────────────────────────────────
if ($WmataApiKey) {
    Write-Host "[2/5] Storing WMATA API key in Key Vault..." -ForegroundColor Yellow

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
    Write-Host "[2/5] Skipping secret storage (no -WmataApiKey provided)" -ForegroundColor DarkGray
    Write-Host "       Run later: az keyvault secret set --vault-name $KeyVaultName --name wmata-api-key --value <key>"
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Grant Function App managed identity Key Vault access
# ─────────────────────────────────────────────────────────────────────────────
Write-Host "[3/5] Granting Function App Key Vault access..." -ForegroundColor Yellow

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
# Step 4: Build, upload, and deploy Function App code
# ─────────────────────────────────────────────────────────────────────────────
if (-not $SkipFunctionDeploy) {
    Write-Host "[4/5] Building Function App package..." -ForegroundColor Yellow

    $buildScript = Join-Path $FunctionProjectPath "build-package.ps1"
    $zipPath     = Join-Path $FunctionProjectPath "dist\wmata-func.zip"

    & $buildScript -OutputPath $zipPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $zipPath)) {
        Write-Error "Package build failed. Ensure Python 3.11 is installed."
        exit 1
    }

    Write-Host "  ✓ Package built: $zipPath" -ForegroundColor Green

    # ── Step 5: Deploy via ARM OneDeploy with user-delegation SAS ──
    Write-Host "[5/5] Deploying to Flex Consumption..." -ForegroundColor Yellow

    # Remove WEBSITE_RUN_FROM_PACKAGE if set from a prior attempt — FC doesn't support it
    $existingSettings = az functionapp config appsettings list `
        --name $FunctionAppName `
        --resource-group $ResourceGroup `
        --query "[?name=='WEBSITE_RUN_FROM_PACKAGE'].name" -o tsv 2>$null

    if ($existingSettings) {
        Write-Host "  Removing unsupported WEBSITE_RUN_FROM_PACKAGE setting..." -ForegroundColor DarkGray
        az functionapp config appsettings delete `
            --name $FunctionAppName `
            --resource-group $ResourceGroup `
            --setting-names WEBSITE_RUN_FROM_PACKAGE `
            --output none
    }

    # Ensure deployer has Storage Blob Data Owner for upload + SAS generation
    $currentUser = az ad signed-in-user show --query id -o tsv
    $storageId   = az storage account show `
        --name $StorageAccountName `
        --resource-group $ResourceGroup `
        --query id -o tsv

    az role assignment create `
        --assignee $currentUser `
        --role "Storage Blob Data Owner" `
        --scope $storageId `
        --output none 2>$null

    # Upload zip to deployment container (identity-based, no shared keys)
    Write-Host "  Uploading package to deployment storage..." -ForegroundColor DarkGray
    az storage blob upload `
        --account-name $StorageAccountName `
        --container-name $DeployContainer `
        --file $zipPath `
        --name $BlobName `
        --auth-mode login `
        --overwrite `
        --output none

    if ($LASTEXITCODE -ne 0) {
        Write-Error "Blob upload failed. You may need to wait ~30s for RBAC to propagate on first run."
        exit 1
    }

    # Generate user-delegation SAS (identity-based, no storage keys)
    $expiry = (Get-Date).AddHours(1).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $sasToken = az storage blob generate-sas `
        --account-name $StorageAccountName `
        --container-name $DeployContainer `
        --name $BlobName `
        --permissions r `
        --expiry $expiry `
        --auth-mode login `
        --as-user `
        -o tsv

    $packageUrl = "https://${StorageAccountName}.blob.core.windows.net/${DeployContainer}/${BlobName}?${sasToken}"

    # Deploy via ARM OneDeploy endpoint (bypasses SCM/Kudu entirely)
    Write-Host "  Triggering deployment via ARM OneDeploy..." -ForegroundColor DarkGray
    $bodyObj  = @{ properties = @{ type = "zip"; packageUri = $packageUrl } }
    $bodyFile = Join-Path $env:TEMP "wmata-deploy-body.json"
    $bodyObj | ConvertTo-Json | Set-Content $bodyFile -Encoding UTF8

    $subscriptionId = (az account show --query id -o tsv)
    $deployUrl = "https://management.azure.com/subscriptions/${subscriptionId}/resourceGroups/${ResourceGroup}/providers/Microsoft.Web/sites/${FunctionAppName}/extensions/onedeploy?api-version=2024-04-01"

    az rest --method PUT --url $deployUrl --headers "Content-Type=application/json" --body "@$bodyFile" --output none 2>$null

    # Clean up temp file
    Remove-Item $bodyFile -ErrorAction SilentlyContinue

    Write-Host "  ✓ Deployment initiated" -ForegroundColor Green

    # Wait for deployment to complete and functions to load
    Write-Host "  Waiting 45s for host startup..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 45

    $functions = az functionapp function list `
        --name $FunctionAppName `
        --resource-group $ResourceGroup `
        --query "[].name" -o tsv 2>$null

    if ($functions) {
        Write-Host "  ✓ Functions loaded:" -ForegroundColor Green
        $functions -split "`n" | ForEach-Object { Write-Host "      $_" -ForegroundColor Green }
    }
    else {
        Write-Host "  ⚠ No functions detected yet — may still be loading. Check:" -ForegroundColor DarkYellow
        Write-Host "    az functionapp function list --name $FunctionAppName --resource-group $ResourceGroup" -ForegroundColor DarkYellow
    }
}
else {
    Write-Host "[4/5] Skipping Function App deploy (--SkipFunctionDeploy)" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host " Deployment Complete" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Redeploy code only:"
Write-Host "    .\infra\deploy-wmata.ps1 -SkipInfraDeploy" -ForegroundColor DarkYellow
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
