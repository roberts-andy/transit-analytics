using './main.bicep'

param location = 'centralus'
param resourceGroupName = 'transit-analytics-rg'
param keyVaultName = 'kvtransitdemo-f70cfb6a'

// Fabric workspace identity (transit-analytics workspace)
param fabricWorkspaceIdentityObjectId = 'd2392ef2-49f8-411b-867f-e59bad3263af'

// Deployer — update with your user object ID
param deployerObjectId = '616f1de1-8350-4b7c-a167-77fd27691566'

param tags = {
  project: 'transit-analytics'
  environment: 'dev'
  managedBy: 'bicep'
}
