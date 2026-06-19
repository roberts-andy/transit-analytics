# Infrastructure — Azure Resources

Deploys the Azure infrastructure for the transit-analytics project using Bicep.

## Resources Created

| Resource | Name | Purpose |
|----------|------|---------|
| Resource Group | `transit-analytics-rg` | Container for all Azure resources |
| Key Vault | `kvtransitdemo-f70cfb6a` | Secure storage for API keys |

## Secrets (Placeholders)

| Secret Name | Description |
|-------------|-------------|
| `mbta-api-key` | MBTA V3 API key |
| `wmata-api-key` | WMATA API key (future) |
| `weather-api-key` | Weather API key (future) |

## RBAC Assignments

| Principal | Role | Purpose |
|-----------|------|---------|
| Deployer (you) | Key Vault Administrator | Manage secrets |
| Fabric workspace identity | Key Vault Secrets User | Read secrets at runtime |

## Deploy

```bash
az deployment sub create \
  --location centralus \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

## Post-Deployment

After deploying, set the actual secret values (since placeholders are empty):

```bash
az keyvault update --name kvtransitdemo-f70cfb6a --resource-group transit-analytics-rg --public-network-access Enabled
az keyvault secret set --vault-name kvtransitdemo-f70cfb6a --name mbta-api-key --value "<your-key>"
az keyvault update --name kvtransitdemo-f70cfb6a --resource-group transit-analytics-rg --public-network-access Disabled
```
