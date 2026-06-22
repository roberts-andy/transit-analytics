<#
.SYNOPSIS
    Full environment setup for transit-analytics.

.DESCRIPTION
    Deploys all Azure and Fabric resources needed to run this project:
    1. Resource Group + Key Vault
    2. WMATA Function App + Event Hub
    3. Fabric Workspace + Lakehouse (bronze)
    4. Schemas (mbta, wmata, weather)
    5. Git integration
    6. Variable Library

.NOTES
    Prerequisites:
    - Azure CLI (az) authenticated
    - Fabric capacity available (F32+ recommended)
    - API keys for MBTA and WMATA

.EXAMPLE
    .\infra\setup.ps1
#>

$ErrorActionPreference = "Stop"

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────
$Location = "centralus"
$ResourceGroup = "transit-analytics-rg"
$FabricWorkspaceName = "transit-analytics"
$GitHubRepo = "transit-analytics"
$GitHubBranch = "main"
$GitDirectory = "/fabric-assets"

Write-Host ""
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " transit-analytics — Environment Setup" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

# Prompt for required values
$KeyVaultName = Read-Host "Enter a globally unique Key Vault name"
$FabricCapacityName = Read-Host "Enter your Fabric capacity name"
$GitHubOwner = Read-Host "Enter your GitHub username (repo owner)"

Write-Host ""
Write-Host "Configuration:" -ForegroundColor Yellow
Write-Host "  Location:        $Location"
Write-Host "  Resource Group:  $ResourceGroup"
Write-Host "  Key Vault:       $KeyVaultName"
Write-Host "  Capacity:        $FabricCapacityName"
Write-Host "  GitHub:          $GitHubOwner/$GitHubRepo ($GitHubBranch)"
Write-Host ""

$confirm = Read-Host "Proceed? (y/N)"
if ($confirm -ne "y" -and $confirm -ne "Y") {
    Write-Host "Aborted." -ForegroundColor Red
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Deploy Azure Infrastructure
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[1/7] Creating resource group..." -ForegroundColor Yellow
az group create --name $ResourceGroup --location $Location -o none

Write-Host "[2/7] Deploying Key Vault..." -ForegroundColor Yellow
$deployerOid = az ad signed-in-user show --query id -o tsv

az keyvault create `
    --name $KeyVaultName `
    --resource-group $ResourceGroup `
    --location $Location `
    --sku standard `
    --enable-rbac-authorization true `
    --enable-soft-delete true `
    --retention-days 90 `
    --bypass AzureServices `
    --default-action Deny `
    -o none

$kvResourceId = az keyvault show --name $KeyVaultName --resource-group $ResourceGroup --query id -o tsv

az role assignment create `
    --assignee $deployerOid `
    --role "Key Vault Administrator" `
    --scope $kvResourceId `
    -o none

Write-Host "  Temporarily enabling public access to create secrets..."
az keyvault update --name $KeyVaultName --resource-group $ResourceGroup --public-network-access Enabled -o none
Start-Sleep -Seconds 5

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Store API Keys
# ─────────────────────────────────────────────────────────────────────────────
Write-Host "[3/7] Creating secrets..." -ForegroundColor Yellow

$mbtaKey = Read-Host "Enter your MBTA API key (or press Enter to skip)" -AsSecureString
$mbtaPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($mbtaKey))
if ($mbtaPlain) {
    az keyvault secret set --vault-name $KeyVaultName --name "mbta-api-key" --value $mbtaPlain -o none
} else {
    az keyvault secret set --vault-name $KeyVaultName --name "mbta-api-key" --value "PLACEHOLDER" -o none
}

$wmataKey = Read-Host "Enter your WMATA API key (or press Enter to skip)" -AsSecureString
$wmataPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($wmataKey))
if ($wmataPlain) {
    az keyvault secret set --vault-name $KeyVaultName --name "wmata-api-key" --value $wmataPlain -o none
} else {
    az keyvault secret set --vault-name $KeyVaultName --name "wmata-api-key" --value "PLACEHOLDER" -o none
}

az keyvault secret set --vault-name $KeyVaultName --name "weather-api-key" --value "PLACEHOLDER" -o none

Write-Host "  Disabling public access..."
az keyvault update --name $KeyVaultName --resource-group $ResourceGroup --public-network-access Disabled -o none

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Create Fabric Workspace
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[4/7] Creating Fabric workspace..." -ForegroundColor Yellow
$fabricToken = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
$fabricHeaders = @{
    "Authorization" = "Bearer $fabricToken"
    "Content-Type"  = "application/json"
}

$capacities = (Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/capacities" -Headers $fabricHeaders).value
$capacity = $capacities | Where-Object { $_.displayName -eq $FabricCapacityName }
if (-not $capacity) {
    Write-Error "Capacity '$FabricCapacityName' not found."
    exit 1
}
$capacityId = $capacity.id
Write-Host "  Capacity ID: $capacityId"

$wsBody = @{ displayName = $FabricWorkspaceName; capacityId = $capacityId } | ConvertTo-Json
$workspace = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces" -Headers $fabricHeaders -Method Post -Body $wsBody
$workspaceId = $workspace.id
Write-Host "  Workspace ID: $workspaceId"

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Provision Workspace Identity & Grant KV Access
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[5/7] Provisioning workspace identity..." -ForegroundColor Yellow
Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$workspaceId/provisionIdentity" -Headers $fabricHeaders -Method Post | Out-Null
Start-Sleep -Seconds 10

$wsDetails = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$workspaceId" -Headers $fabricHeaders
$wsIdentitySp = $wsDetails.workspaceIdentity.servicePrincipalId
Write-Host "  Workspace identity SP: $wsIdentitySp"

az role assignment create `
    --assignee $wsIdentitySp `
    --role "Key Vault Secrets User" `
    --scope $kvResourceId `
    -o none

Write-Host "  ✓ Key Vault access granted" -ForegroundColor Green

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Create Lakehouse
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[6/7] Creating bronze lakehouse..." -ForegroundColor Yellow
$fabricToken = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
$fabricHeaders = @{ "Authorization" = "Bearer $fabricToken"; "Content-Type" = "application/json" }

$lhBody = @{ displayName = "bronze"; type = "Lakehouse" } | ConvertTo-Json
$lakehouse = Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$workspaceId/items" -Headers $fabricHeaders -Method Post -Body $lhBody
$lakehouseId = $lakehouse.id
Write-Host "  Lakehouse ID: $lakehouseId"
Write-Host "  Waiting for SQL endpoint provisioning..."
Start-Sleep -Seconds 30

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Connect to Git
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[7/7] Connecting workspace to GitHub..." -ForegroundColor Yellow
$gitConnId = Read-Host "Enter your Fabric GitHub connection ID (or press Enter to skip)"

if ($gitConnId) {
    $fabricToken = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
    $fabricHeaders = @{ "Authorization" = "Bearer $fabricToken"; "Content-Type" = "application/json" }

    $gitBody = @{
        gitProviderDetails = @{
            gitProviderType = "GitHub"
            ownerName       = $GitHubOwner
            repositoryName  = $GitHubRepo
            branchName      = $GitHubBranch
            directoryName   = $GitDirectory
        }
        myGitCredentials = @{
            source       = "ConfiguredConnection"
            connectionId = $gitConnId
        }
    } | ConvertTo-Json -Depth 4

    Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$workspaceId/git/connect" -Headers $fabricHeaders -Method Post -Body $gitBody | Out-Null

    $initBody = @{ initializationStrategy = "PreferRemote" } | ConvertTo-Json
    Invoke-RestMethod -Uri "https://api.fabric.microsoft.com/v1/workspaces/$workspaceId/git/initializeConnection" -Headers $fabricHeaders -Method Post -Body $initBody | Out-Null

    Write-Host "  ✓ Git connected and synced" -ForegroundColor Green
} else {
    Write-Host "  Skipped — configure git integration manually in Fabric" -ForegroundColor DarkGray
}

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host " Setup Complete!" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Azure:"
Write-Host "    Resource Group:  $ResourceGroup"
Write-Host "    Key Vault:       $KeyVaultName (https://$KeyVaultName.vault.azure.net/)"
Write-Host ""
Write-Host "  Fabric:"
Write-Host "    Workspace:       $FabricWorkspaceName ($workspaceId)"
Write-Host "    Lakehouse:       bronze ($lakehouseId)"
Write-Host "    Identity SP:     $wsIdentitySp"
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    1. Deploy WMATA Function:  .\infra\deploy-wmata.ps1 -WmataApiKey '<key>'"
Write-Host "    2. Trigger reference load: .\infra\run-wmata-pipeline.ps1 -WorkspaceId '$workspaceId'"
Write-Host "    3. Create Eventstream in Fabric UI pointing to wmata-ingest-ehns/wmata-realtime"
Write-Host ""
