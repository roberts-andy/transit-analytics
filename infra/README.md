# Infrastructure — Azure Resources

Deploys the Azure infrastructure for the transit-analytics project using Bicep.

## Resources

| Resource | Name | Purpose |
|----------|------|---------|
| Resource Group | `transit-analytics-rg` | Container for all Azure resources |
| Key Vault | `kvtransitdemo-f70cfb6a` | Secure storage for API keys (existing, not created by Bicep) |
| Function App | `wmata-ingest-func` | Flex Consumption function for WMATA real-time polling |
| App Service Plan | `wmata-ingest-plan` | Flex Consumption (FC1) hosting plan |
| Event Hub Namespace | `wmata-ingest-ehns` | Event Hub namespace for real-time events |
| Event Hub | `wmata-realtime` | Hub for WMATA position/prediction/incident events |
| Storage Account | `wmataingeststor` | Function App deployment storage + diagnostics |
| Application Insights | `wmata-ingest-appinsights` | Monitoring and diagnostics |

## Key Vault Secrets

| Secret Name | Description |
|-------------|-------------|
| `mbta-api-key` | MBTA V3 API key |
| `wmata-api-key` | WMATA API key |

## Event Hub Auth Rules

| Rule | Key | Purpose |
|------|-----|---------|
| `function-send` | Send | Used by the Azure Function to publish events |
| `fabric-listen` | Listen | Used by Fabric Eventstream to consume events |

Consumer group: `fabric-eventstream`

## Deployment Scripts

| Script | Purpose |
|--------|---------|
| `deploy-wmata.ps1` | Full deployment: infra + KV secret + function code |
| `deploy.ps1` | Bicep-only deployment |
| `setup.ps1` | First-time environment setup |
| `run-wmata-pipeline.ps1` | Trigger the WMATA reference data pipeline in Fabric |

## Deploy

```powershell
# Full deployment (infra + function code)
.\infra\deploy-wmata.ps1

# Skip infra, just redeploy function code
.\infra\deploy-wmata.ps1 -SkipInfraDeploy

# Bicep only
.\infra\deploy.ps1
```

### Deployment Notes

- **Location**: Subscription-level deployment uses `eastus` (pinned by existing deployment name "main")
- **Key Vault**: Referenced as `existing` — not created by Bicep (already provisioned)
- **Storage**: `allowSharedKeyAccess` is blocked by subscription policy; all auth is identity-based
- **Function deploy**: Uses ARM OneDeploy endpoint with a user-delegation SAS (bypasses SCM/Kudu)

## RBAC Assignments (managed by Bicep)

| Principal | Role | Scope |
|-----------|------|-------|
| Deployer (you) | Key Vault Administrator | Key Vault |
| Fabric workspace identity | Key Vault Secrets User | Key Vault |
| Function App MI | Storage Blob Data Owner | Storage Account |
| Function App MI | Key Vault Secrets User | Key Vault |

