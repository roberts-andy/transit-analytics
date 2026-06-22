#!/bin/bash
# Store WMATA API key in Key Vault
# Run once after initial deployment: ./setup-wmata-secret.sh <your-api-key>

set -euo pipefail

KEYVAULT_NAME="kvtransitdemo-f70cfb6a"
SECRET_NAME="wmata-api-key"

if [ -z "${1:-}" ]; then
  echo "Usage: $0 <wmata-api-key>"
  exit 1
fi

echo "Storing WMATA API key in Key Vault: $KEYVAULT_NAME"
az keyvault secret set \
  --vault-name "$KEYVAULT_NAME" \
  --name "$SECRET_NAME" \
  --value "$1" \
  --output none

echo "✓ Secret '$SECRET_NAME' stored in '$KEYVAULT_NAME'"
echo ""
echo "Grant the Function App managed identity access:"
echo "  az keyvault set-policy --name $KEYVAULT_NAME \\"
echo "    --object-id <function-app-principal-id> \\"
echo "    --secret-permissions get"
