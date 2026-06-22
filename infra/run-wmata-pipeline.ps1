<#
.SYNOPSIS
    Deploys the full transit-analytics Fabric pipeline using Copy Activity.

.DESCRIPTION
    Triggers the WMATA reference data pipeline in Fabric to run the
    Copy Activity that pulls stations, routes, lines, and stops into
    bronze.wmata.* Delta tables.

    This script calls the Fabric REST API to trigger the pipeline.

.PARAMETER WorkspaceId
    Fabric workspace ID (GUID).

.PARAMETER PipelineName
    Name of the Data Pipeline to trigger. Defaults to pl_wmata_reference_daily.

.EXAMPLE
    .\infra\run-wmata-pipeline.ps1 -WorkspaceId "c030c477-6e50-4334-8fcb-fd032f8870b9"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$WorkspaceId,

    [string]$PipelineName = "pl_wmata_reference_daily"
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host " WMATA Reference Data Pipeline — Trigger" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""

# Get Fabric access token
Write-Host "Acquiring Fabric access token..." -ForegroundColor Yellow
$token = az account get-access-token --resource "https://api.fabric.microsoft.com" --query accessToken -o tsv
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to get access token. Run 'az login' first."
    exit 1
}

$headers = @{
    "Authorization" = "Bearer $token"
    "Content-Type"  = "application/json"
}

# Find the pipeline item
Write-Host "Looking up pipeline '$PipelineName' in workspace..." -ForegroundColor Yellow
$itemsUrl = "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/items?type=DataPipeline"
$items = Invoke-RestMethod -Uri $itemsUrl -Headers $headers -Method Get

$pipeline = $items.value | Where-Object { $_.displayName -eq $PipelineName }
if (-not $pipeline) {
    Write-Error "Pipeline '$PipelineName' not found in workspace $WorkspaceId"
    Write-Host "Available pipelines:"
    $items.value | ForEach-Object { Write-Host "  - $($_.displayName)" }
    exit 1
}

$pipelineId = $pipeline.id
Write-Host "  Found pipeline: $PipelineName ($pipelineId)" -ForegroundColor Green

# Trigger the pipeline run
Write-Host "Triggering pipeline run..." -ForegroundColor Yellow

# Fetch WMATA API key from Key Vault to pass as pipeline parameter
$wmataKey = az keyvault secret show `
    --vault-name "kvtransitdemo-f70cfb6a" `
    --name "wmata-api-key" `
    --query value -o tsv

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to retrieve WMATA API key from Key Vault."
    exit 1
}

$body = @{
    parameters = @{
        wmata_api_key = $wmataKey
    }
} | ConvertTo-Json -Depth 3

$runUrl = "https://api.fabric.microsoft.com/v1/workspaces/$WorkspaceId/items/$pipelineId/jobs/instances?jobType=Pipeline"
$response = Invoke-RestMethod -Uri $runUrl -Headers $headers -Method Post -Body $body

Write-Host ""
Write-Host "  ✓ Pipeline triggered successfully" -ForegroundColor Green
Write-Host ""
Write-Host "  Monitor in Fabric UI:"
Write-Host "    https://app.fabric.microsoft.com/groups/$WorkspaceId/pipelines/$pipelineId"
Write-Host ""
Write-Host "  Data will land in:"
Write-Host "    bronze.wmata.stations"
Write-Host "    bronze.wmata.bus_routes"
Write-Host "    bronze.wmata.rail_lines"
Write-Host "    bronze.wmata.station_entrances"
Write-Host "    bronze.wmata.bus_stops"
Write-Host ""
