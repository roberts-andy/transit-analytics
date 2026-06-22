@description('Key Vault name')
param keyVaultName string

@description('Azure region')
param location string

@description('Fabric workspace identity object ID — granted Key Vault Secrets User')
param fabricWorkspaceIdentityObjectId string

@description('Deployer object ID — granted Key Vault Administrator for initial setup')
param deployerObjectId string

@description('Tags')
param tags object

// Key Vault — private by default, trusted services bypass enabled
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enablePurgeProtection: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Deny'
    }
  }
}

// Secret placeholders (empty values — populate manually or via pipeline)
resource secretMbtaApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'mbta-api-key'
  properties: {
    value: '' // Placeholder — set actual value post-deployment
    contentType: 'API Key'
    attributes: {
      enabled: true
    }
  }
}

resource secretWmataApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'wmata-api-key'
  properties: {
    value: '' // Placeholder — set actual value post-deployment
    contentType: 'API Key'
    attributes: {
      enabled: true
    }
  }
}

resource secretWeatherApiKey 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: kv
  name: 'weather-api-key'
  properties: {
    value: '' // Placeholder — set actual value post-deployment
    contentType: 'API Key'
    attributes: {
      enabled: true
    }
  }
}

// Role: Key Vault Administrator → deployer (manage secrets during setup)
resource roleDeployerAdmin 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, deployerObjectId, '00482a5a-887f-4fb3-b363-3b7fe8e74483')
  scope: kv
  properties: {
    principalId: deployerObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '00482a5a-887f-4fb3-b363-3b7fe8e74483') // Key Vault Administrator
    principalType: 'User'
  }
}

// Role: Key Vault Secrets User → Fabric workspace identity (read secrets at runtime)
resource roleFabricSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(kv.id, fabricWorkspaceIdentityObjectId, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: kv
  properties: {
    principalId: fabricWorkspaceIdentityObjectId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6') // Key Vault Secrets User
    principalType: 'ServicePrincipal'
  }
}

output keyVaultUri string = kv.properties.vaultUri
output keyVaultName string = kv.name
