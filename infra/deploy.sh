#!/bin/bash
# Deploy Azure infrastructure for transit-analytics
# Run from the repo root

set -e

echo "Deploying transit-analytics infrastructure..."
az deployment sub create \
  --location centralus \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam

echo "Deployment complete."
