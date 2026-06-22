targetScope = 'subscription'

@description('Azure region for all resources')
param location string = 'centralus'

@description('Resource group name')
param resourceGroupName string = 'transit-analytics-rg'

@description('Key Vault name (globally unique)')
param keyVaultName string = 'kvtransitdemo-f70cfb6a'

@description('Object ID of the Fabric workspace identity to grant Key Vault access')
param fabricWorkspaceIdentityObjectId string

@description('Object ID of the deploying user/service principal for initial KV admin access')
param deployerObjectId string

@description('Tags applied to all resources')
param tags object = {
  project: 'transit-analytics'
  environment: 'dev'
  managedBy: 'bicep'
}

// Resource Group
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Deploy Key Vault and role assignments into the resource group
module keyVault 'modules/keyvault.bicep' = {
  name: 'deploy-keyvault'
  scope: rg
  params: {
    keyVaultName: keyVaultName
    location: location
    fabricWorkspaceIdentityObjectId: fabricWorkspaceIdentityObjectId
    deployerObjectId: deployerObjectId
    tags: tags
  }
}

// Deploy WMATA real-time ingestion infrastructure (Function App + Event Hub)
module wmataFunction 'modules/wmata-function.bicep' = {
  name: 'deploy-wmata-function'
  scope: rg
  params: {
    location: location
    namePrefix: 'wmata-ingest'
    keyVaultName: keyVaultName
    tags: tags
  }
}

output resourceGroupId string = rg.id
output keyVaultUri string = keyVault.outputs.keyVaultUri
output keyVaultName string = keyVault.outputs.keyVaultName
output wmataFunctionAppName string = wmataFunction.outputs.functionAppName
output wmataEventHubNamespace string = wmataFunction.outputs.eventHubNamespaceName
output wmataEventHubName string = wmataFunction.outputs.eventHubName
