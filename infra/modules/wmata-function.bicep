@description('Azure region for the Function App and Event Hub')
param location string = 'eastus'

@description('Name prefix for all WMATA ingestion resources')
param namePrefix string = 'wmata-ingest'

@description('Key Vault name to reference for secrets')
param keyVaultName string

@description('Key Vault resource group')
param keyVaultResourceGroup string

@description('Tags applied to all resources')
param tags object = {
  project: 'transit-analytics'
  component: 'wmata-realtime-ingest'
  managedBy: 'bicep'
}

// Event Hub Namespace
resource eventHubNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: '${namePrefix}-ehns'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
    capacity: 1
  }
  properties: {
    isAutoInflateEnabled: true
    maximumThroughputUnits: 4
  }
}

// Event Hub
resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventHubNamespace
  name: 'wmata-realtime'
  properties: {
    messageRetentionInDays: 1
    partitionCount: 4
  }
}

// Consumer group for Fabric Eventstream
resource consumerGroup 'Microsoft.EventHub/namespaces/eventhubs/consumergroups@2024-01-01' = {
  parent: eventHub
  name: 'fabric-eventstream'
}

// Event Hub authorization rule for the Function App (Send)
resource ehSendRule 'Microsoft.EventHub/namespaces/eventhubs/authorizationRules@2024-01-01' = {
  parent: eventHub
  name: 'function-send'
  properties: {
    rights: ['Send']
  }
}

// Event Hub authorization rule for Fabric Eventstream (Listen)
resource ehListenRule 'Microsoft.EventHub/namespaces/eventhubs/authorizationRules@2024-01-01' = {
  parent: eventHub
  name: 'fabric-listen'
  properties: {
    rights: ['Listen']
  }
}

// Storage account for Function App
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: replace('${namePrefix}stor', '-', '')
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
  }
}

// App Service Plan (Flex Consumption for near-zero cold starts)
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${namePrefix}-plan'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true // Linux
  }
}

// Application Insights
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-insights'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    Request_Source: 'rest'
  }
}

// Function App
resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: '${namePrefix}-func'
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      pythonVersion: '3.11'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=core.windows.net;AccountKey=${storageAccount.listKeys().keys[0].value}'
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'WMATA_API_KEY'
          value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=wmata-api-key)'
        }
        {
          name: 'EVENT_HUB_CONNECTION'
          value: ehSendRule.listKeys().primaryConnectionString
        }
        {
          name: 'EVENT_HUB_NAME'
          value: eventHub.name
        }
      ]
    }
  }
}

// Grant Function App access to Key Vault (Key Vault Secrets User role)
resource keyVaultRef 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
  scope: resourceGroup(keyVaultResourceGroup)
}

output functionAppName string = functionApp.name
output functionAppPrincipalId string = functionApp.identity.principalId
output eventHubNamespaceName string = eventHubNamespace.name
output eventHubName string = eventHub.name
output eventHubListenConnectionString string = ehListenRule.listKeys().primaryConnectionString
output appInsightsInstrumentationKey string = appInsights.properties.InstrumentationKey
