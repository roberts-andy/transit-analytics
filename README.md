# Transit Analytics

Real-time and batch ingestion of public transit data into Microsoft Fabric Lakehouse for analysis.

## Data Sources

| Agency | Batch (Reference Data) | Real-Time (Streaming) |
|--------|----------------------|----------------------|
| **MBTA** (Boston) | Spark notebooks (full load) | Spark notebooks (SSE streaming) |
| **WMATA** (Washington DC) | Fabric Data Pipeline (Copy Activity) | Azure Function → Event Hub → Eventstream |

## Project Structure

```
transit-analytics/
├── fabric-assets/
│   ├── mbta/                          # MBTA Fabric artifacts (notebooks, pipelines)
│   ├── wmata/
│   │   ├── pl_wmata_reference_daily/  # Data Pipeline — 5 REST endpoints → bronze.wmata.*
│   │   └── es_wmata_realtime/         # Eventstream — Event Hub → bronze.wmata.realtime_events
│   └── transit-analytics-config.VariableLibrary/
├── functions/
│   └── wmata-realtime-ingest/         # Azure Function (Python 3.11, Flex Consumption)
│       ├── function_app.py            # Two timer triggers: high-freq (20s), incidents (60s)
│       ├── build-package.ps1          # Build deployment zip with Python 3.11 venv
│       └── README.md
├── infra/
│   ├── main.bicep                     # Top-level Bicep orchestrator
│   ├── modules/
│   │   ├── keyvault.bicep             # Key Vault reference + RBAC
│   │   └── wmata-function.bicep       # Function App + Event Hub + Storage + App Insights
│   ├── deploy-wmata.ps1               # Full WMATA deployment (infra + code)
│   ├── deploy.ps1                     # Bicep-only deployment
│   ├── run-wmata-pipeline.ps1         # Trigger reference data pipeline
│   └── README.md
└── README.md                          # ← You are here
```

## WMATA Ingestion Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  BATCH (Daily)                                                       │
│                                                                       │
│  Fabric Data Pipeline ──→ WMATA REST API ──→ bronze.wmata.*          │
│  (Copy Activity)           /Stations, /Routes     (Delta tables)     │
│                            /Lines, /Stops                            │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│  REAL-TIME (every 20-60s)                                            │
│                                                                       │
│  Azure Function ──→ WMATA REST API ──→ Event Hub ──→ Eventstream    │
│  (Timer triggers)   /Predictions        wmata-realtime   │           │
│                     /BusPositions                         ▼           │
│                     /Incidents           bronze.wmata.realtime_events │
└─────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Azure CLI (`az login`)
- Python 3.11 (`winget install Python.Python.3.11`)
- PowerShell 7+

### Deploy WMATA Infrastructure + Function

```powershell
# First-time: deploy everything
.\infra\deploy-wmata.ps1

# Code changes only: skip Bicep
.\infra\deploy-wmata.ps1 -SkipInfraDeploy
```

### Run Reference Data Pipeline

```powershell
.\infra\run-wmata-pipeline.ps1
```

## Key Configuration

| Setting | Value |
|---------|-------|
| Resource Group | `transit-analytics-rg` |
| Key Vault | `kvtransitdemo-f70cfb6a` |
| Function App | `wmata-ingest-func` (Flex Consumption) |
| Event Hub | `wmata-ingest-ehns` / `wmata-realtime` |
| Fabric Workspace | `c030c477-6e50-4334-8fcb-fd032f8870b9` |
| Bronze Lakehouse | `82a4dd04-28a2-4ac4-acab-254953df7edb` |

## Deployment Notes

The subscription has an Azure Policy that blocks `allowSharedKeyAccess` on storage accounts. All deployment and runtime auth uses **Managed Identity** and **user-delegation SAS** tokens. See [functions/wmata-realtime-ingest/README.md](functions/wmata-realtime-ingest/README.md) for details on the deployment approach.
