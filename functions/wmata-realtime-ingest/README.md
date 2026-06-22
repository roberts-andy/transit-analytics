# WMATA Real-Time Ingestion — Azure Function

Timer-triggered Azure Function that polls WMATA real-time REST APIs and publishes events to Azure Event Hub for consumption by Fabric Eventstream.

## Endpoints Polled

| Function | WMATA Endpoint | Refresh Rate |
|----------|---------------|-------------|
| `wmata_realtime_high_freq` | `/StationPrediction.svc/json/GetPrediction/All` | ~20s |
| `wmata_realtime_high_freq` | `/Bus.svc/json/jBusPositions` | ~20s |
| `wmata_realtime_incidents` | `/Incidents.svc/json/Incidents` | ~60s |
| `wmata_realtime_incidents` | `/Incidents.svc/json/BusIncidents` | ~60s |

## Architecture

```
Azure Function (Timer: 20s / 60s)
    → polls WMATA REST APIs (api_key header auth)
    → publishes JSON events to Event Hub (partitioned by feed type)
    → Fabric Eventstream consumes from Event Hub
    → Lands in bronze.wmata.realtime_events Delta table
```

## Hosting

- **Plan**: Flex Consumption (FC1) — scales to zero, near-zero cold starts
- **Runtime**: Python 3.11
- **Storage auth**: Managed Identity (no shared keys — blocked by subscription policy)

## Configuration

All secrets are stored in Azure Key Vault (`kvtransitdemo-f70cfb6a`):

| App Setting | Source | Description |
|-------------|--------|-------------|
| `WMATA_API_KEY` | Key Vault reference | WMATA developer API key |
| `EVENT_HUB_CONNECTION__fullyQualifiedNamespace` | Bicep | Event Hub namespace FQDN |
| `EVENT_HUB_NAME` | Bicep | Event Hub name (`wmata-realtime`) |
| `AzureWebJobsStorage__accountName` | Bicep | Identity-based storage connection |

## Build & Deploy

### Prerequisites
- Python 3.11 (`winget install Python.Python.3.11`)
- Azure CLI (`az login`)

### Build the package

```powershell
cd functions\wmata-realtime-ingest
.\build-package.ps1
```

This creates `dist\wmata-func.zip` with all dependencies bundled. The `.venv` is reused across builds for speed.

### Deploy

```powershell
# Full deployment (infra + code)
.\infra\deploy-wmata.ps1

# Code-only redeploy
.\infra\deploy-wmata.ps1 -SkipInfraDeploy
```

The deploy script:
1. Builds the zip package via `build-package.ps1`
2. Uploads to blob storage (identity-based auth)
3. Generates a user-delegation SAS token
4. Calls the ARM OneDeploy endpoint (bypasses SCM/Kudu)

### Why not `func publish`?

The subscription's Azure Policy blocks `allowSharedKeyAccess` on storage accounts. This breaks:
- `func azure functionapp publish` (uses storage keys for remote build)
- `az functionapp deployment source config-zip` (Kudu uses storage keys internally)
- `az functionapp deploy --type zip` (SCM endpoint returns 415 on FC)
- `WEBSITE_RUN_FROM_PACKAGE` (not supported on Flex Consumption)

The ARM OneDeploy + user-delegation SAS approach is fully identity-based.

## Local Development

```powershell
cd functions\wmata-realtime-ingest
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Create local.settings.json with your API key and Event Hub connection
func start
```

## Monitoring

```powershell
# Check function status
az functionapp function list --name wmata-ingest-func --resource-group transit-analytics-rg -o table

# View live logs (App Insights)
az monitor app-insights query --app wmata-ingest-appinsights --resource-group transit-analytics-rg --analytics-query "traces | order by timestamp desc | take 20"
```
