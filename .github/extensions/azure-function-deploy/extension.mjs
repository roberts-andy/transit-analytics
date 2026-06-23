import { joinSession } from "@github/copilot-sdk/extension";

await joinSession({
    tools: [
        {
            name: "azure_function_deploy_guide",
            description: "Returns the complete deployment pattern for Azure Functions on Flex Consumption (FC1) in environments where storage shared key access is blocked. Use when deploying function apps, building deployment scripts, or troubleshooting deployment failures.",
            parameters: {
                type: "object",
                properties: {
                    topic: {
                        type: "string",
                        description: "Specific topic: 'full-pattern', 'bicep-config', 'troubleshooting', or 'build-script'",
                        enum: ["full-pattern", "bicep-config", "troubleshooting", "build-script"],
                    },
                },
            },
            handler: async (args) => {
                const guides = {
                    "full-pattern": `# Azure Function FC1 Deployment Pattern (Identity-Based)

## Constraints
- Azure Policy blocks allowSharedKeyAccess on all storage accounts
- func publish, az functionapp deploy, Kudu, WEBSITE_RUN_FROM_PACKAGE all FAIL
- Must use: ARM OneDeploy + user-delegation SAS

## Steps (PowerShell)

### 1. Build zip locally (Python 3.11 venv)
\`\`\`powershell
$venvDir = Join-Path $functionProjectPath ".venv"
if (-not (Test-Path "$venvDir\\Scripts\\python.exe")) { py -3.11 -m venv $venvDir }
$venvPip = "$venvDir\\Scripts\\pip.exe"
$stageDir = "$env:TEMP\\func-stage"
if (Test-Path $stageDir) { Remove-Item -Recurse -Force $stageDir }
New-Item -ItemType Directory $stageDir | Out-Null
& $venvPip install -r requirements.txt --target "$stageDir\\.python_packages\\lib\\site-packages" --quiet --upgrade
Copy-Item "function_app.py","host.json","requirements.txt" -Destination $stageDir
Compress-Archive -Path "$stageDir\\*" -DestinationPath $zipPath
\`\`\`

### 2. Upload to blob (identity auth)
\`\`\`powershell
az storage blob upload --account-name $storageAccount --container-name $container --file $zipPath --name $blobName --auth-mode login --overwrite --output none
\`\`\`

### 3. Generate user-delegation SAS
\`\`\`powershell
$expiry = (Get-Date).AddHours(1).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$sas = az storage blob generate-sas --account-name $storageAccount --container-name $container --name $blobName --permissions r --expiry $expiry --auth-mode login --as-user -o tsv
$url = "https://$storageAccount.blob.core.windows.net/$container/$blobName?$sas"
\`\`\`

### 4. Call ARM OneDeploy
\`\`\`powershell
$body = @{ properties = @{ type = "zip"; packageUri = $url } } | ConvertTo-Json
$bodyFile = "$env:TEMP\\deploy-body.json"; $body | Set-Content $bodyFile -Encoding UTF8
$subId = az account show --query id -o tsv
az rest --method PUT --url "https://management.azure.com/subscriptions/$subId/resourceGroups/$rg/providers/Microsoft.Web/sites/$appName/extensions/onedeploy?api-version=2024-04-01" --headers "Content-Type=application/json" --body "@$bodyFile" --output none
Remove-Item $bodyFile
\`\`\`

### 5. Verify (wait 45s for cold start)
\`\`\`powershell
Start-Sleep -Seconds 45
az functionapp function list --name $appName --resource-group $rg -o table
\`\`\`

## Prerequisites
- Deployer needs: Storage Blob Data Owner (on storage account)
- Function App MI needs: Storage Blob Data Owner (Bicep grants this)
- Remove any leftover WEBSITE_RUN_FROM_PACKAGE setting before deploying`,

                    "bicep-config": `# Bicep Configuration for Flex Consumption Function App

## App Service Plan (FC1)
\`\`\`bicep
resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: '\${namePrefix}-plan'
  location: location
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  kind: 'functionapp'
  properties: { reserved: true }
}
\`\`\`

## Function App (MUST include functionAppConfig)
\`\`\`bicep
resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '\${storageAccount.properties.primaryEndpoints.blob}deploymentpackage'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.11' }
    }
    siteConfig: {
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storageAccount.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
      ]
    }
  }
}
\`\`\`

## Storage Account (no shared keys)
\`\`\`bicep
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowSharedKeyAccess: false  // or true if policy will override anyway
  }
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  name: '\${storageAccount.name}/default/deploymentpackage'
}
\`\`\`

## Required Role Assignment
\`\`\`bicep
resource storageBlobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, functionApp.id, 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
  scope: storageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}
\`\`\``,

                    "troubleshooting": `# Troubleshooting Azure Function FC1 Deployment

| Symptom | Cause | Fix |
|---------|-------|-----|
| "WEBSITE_RUN_FROM_PACKAGE is set" during deploy | Stale setting from prior attempt | az functionapp config appsettings delete --setting-names WEBSITE_RUN_FROM_PACKAGE |
| Status 415 from az functionapp deploy | SCM endpoint incompatible with FC | Use ARM OneDeploy endpoint instead |
| PackageUriDownloadException: 409 | Blob URL unsigned, public access disabled | Generate user-delegation SAS |
| RunFromExternalUrlException | WEBSITE_RUN_FROM_PACKAGE still set | Remove the app setting, then redeploy |
| Functions list empty after deploy | Cold start / import errors | Wait 45-60s. Check App Insights for Python import errors |
| "Key based authentication not permitted" | Storage shared key access blocked | Use --auth-mode login for all storage operations |
| functionAppConfig missing error | FC1 plan requires functionAppConfig in site properties | Add deployment.storage, scaleAndConcurrency, runtime blocks |
| Blob upload 403 | Missing Storage Blob Data Owner role | Grant role, wait 30s for propagation |

## Diagnostic Commands
\`\`\`powershell
# Check function app state
az functionapp show --name $app --resource-group $rg --query state -o tsv

# List loaded functions
az functionapp function list --name $app --resource-group $rg -o table

# Check app settings
az functionapp config appsettings list --name $app --resource-group $rg -o table

# Check deployment history
az rest --method GET --url "https://management.azure.com/subscriptions/$subId/resourceGroups/$rg/providers/Microsoft.Web/sites/$app/deployments?api-version=2024-04-01"

# Live logs (App Insights)
az monitor app-insights query --app $insightsName --resource-group $rg --analytics-query "traces | order by timestamp desc | take 20"
\`\`\``,

                    "build-script": `# Build Script Template (build-package.ps1)

Place this in the function project folder alongside function_app.py.

Key features:
- Reuses persistent .venv (fast subsequent builds)
- Auto-detects Python 3.11 via py launcher
- Installs deps into .python_packages layout (Azure Functions convention)
- Only stages source files (not .venv or __pycache__)

\`\`\`powershell
[CmdletBinding()]
param([string]$OutputPath)

$ErrorActionPreference = "Stop"
$scriptDir = $PSScriptRoot
if (-not $OutputPath) { $OutputPath = Join-Path $scriptDir "dist\\func-package.zip" }
$outputDir = Split-Path $OutputPath -Parent
if (-not (Test-Path $outputDir)) { New-Item -ItemType Directory $outputDir -Force | Out-Null }

# Reuse .venv
$venvDir = Join-Path $scriptDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating Python 3.11 venv..." -ForegroundColor Cyan
    py -3.11 -m venv $venvDir
    & $venvPython -m pip install --upgrade pip --quiet 2>&1 | Out-Null
} else {
    Write-Host "Reusing .venv" -ForegroundColor Cyan
}

# Stage
$stageDir = Join-Path $env:TEMP "func-stage-\$(Get-Random)"
New-Item -ItemType Directory $stageDir | Out-Null
try {
    $packagesDir = Join-Path $stageDir ".python_packages\\lib\\site-packages"
    & "$venvDir\\Scripts\\pip.exe" install -r (Join-Path $scriptDir "requirements.txt") --target $packagesDir --quiet --upgrade
    Copy-Item (Join-Path $scriptDir "function_app.py"),(Join-Path $scriptDir "host.json"),(Join-Path $scriptDir "requirements.txt") -Destination $stageDir
    if (Test-Path "$scriptDir\\function.json") { Copy-Item "$scriptDir\\function.json" -Destination $stageDir }
    if (Test-Path $OutputPath) { Remove-Item $OutputPath -Force }
    Compress-Archive -Path "$stageDir\\*" -DestinationPath $OutputPath
    Write-Host "Built: $OutputPath (\$([math]::Round((Get-Item $OutputPath).Length/1MB, 2)) MB)" -ForegroundColor Green
} finally {
    Remove-Item -Recurse -Force $stageDir -ErrorAction SilentlyContinue
}
\`\`\`

## .gitignore entries needed
\`\`\`
.venv/
dist/
.python_packages/
__pycache__/
\`\`\``,
                };

                const topic = args?.topic || "full-pattern";
                return guides[topic] || guides["full-pattern"];
            },
        },
    ],
});
