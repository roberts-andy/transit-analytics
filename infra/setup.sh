#!/bin/bash
# =============================================================================
# transit-analytics — Full Environment Setup
# =============================================================================
# Deploys all Azure and Fabric resources needed to run this project.
# Run from the repo root after forking.
#
# Prerequisites:
#   - Azure CLI (az) authenticated with an active subscription
#   - Fabric capacity available (F32+ recommended)
#   - MBTA API key (register at https://api-v3.mbta.com)
#
# Usage:
#   bash infra/setup.sh
# =============================================================================

set -e

# ---------------------------------------------------------------------------
# Configuration — update these for your environment
# ---------------------------------------------------------------------------
LOCATION="centralus"
RESOURCE_GROUP="transit-analytics-rg"
KEYVAULT_NAME=""  # Must be globally unique (3-24 chars, alphanumeric + hyphens)
FABRIC_CAPACITY_NAME=""  # Your Fabric capacity name
FABRIC_WORKSPACE_NAME="transit-analytics"
GITHUB_OWNER=""  # Your GitHub username (owner of the forked repo)
GITHUB_REPO="transit-analytics"
GITHUB_BRANCH="main"
GIT_DIRECTORY="/fabric-assets"

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
echo "=============================================="
echo " transit-analytics — Environment Setup"
echo "=============================================="
echo ""

if [ -z "$KEYVAULT_NAME" ]; then
  read -p "Enter a globally unique Key Vault name: " KEYVAULT_NAME
fi
if [ -z "$FABRIC_CAPACITY_NAME" ]; then
  read -p "Enter your Fabric capacity name: " FABRIC_CAPACITY_NAME
fi
if [ -z "$GITHUB_OWNER" ]; then
  read -p "Enter your GitHub username (repo owner): " GITHUB_OWNER
fi

echo ""
echo "Configuration:"
echo "  Location:          $LOCATION"
echo "  Resource Group:    $RESOURCE_GROUP"
echo "  Key Vault:         $KEYVAULT_NAME"
echo "  Fabric Capacity:   $FABRIC_CAPACITY_NAME"
echo "  GitHub:            $GITHUB_OWNER/$GITHUB_REPO ($GITHUB_BRANCH)"
echo ""
read -p "Proceed? (y/N): " CONFIRM
if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  echo "Aborted."
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: Deploy Azure Infrastructure
# ---------------------------------------------------------------------------
echo ""
echo "[1/7] Creating resource group..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" -o none

echo "[2/7] Deploying Key Vault..."
DEPLOYER_OID=$(az ad signed-in-user show --query id -o tsv)

az keyvault create \
  --name "$KEYVAULT_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku standard \
  --enable-rbac-authorization true \
  --enable-soft-delete true \
  --retention-days 90 \
  --bypass AzureServices \
  --default-action Deny \
  -o none

# Grant deployer Key Vault Administrator
KV_RESOURCE_ID=$(az keyvault show --name "$KEYVAULT_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)
az role assignment create \
  --assignee "$DEPLOYER_OID" \
  --role "Key Vault Administrator" \
  --scope "$KV_RESOURCE_ID" \
  -o none

echo "  Temporarily enabling public access to create secrets..."
az keyvault update --name "$KEYVAULT_NAME" --resource-group "$RESOURCE_GROUP" --public-network-access Enabled -o none
sleep 5

# Create secret placeholders
echo "[3/7] Creating secret placeholders..."
read -sp "Enter your MBTA API key (or press Enter to skip): " MBTA_KEY
echo ""
if [ -n "$MBTA_KEY" ]; then
  az keyvault secret set --vault-name "$KEYVAULT_NAME" --name "mbta-api-key" --value "$MBTA_KEY" -o none
else
  az keyvault secret set --vault-name "$KEYVAULT_NAME" --name "mbta-api-key" --value "PLACEHOLDER" -o none
fi
az keyvault secret set --vault-name "$KEYVAULT_NAME" --name "wmata-api-key" --value "PLACEHOLDER" -o none
az keyvault secret set --vault-name "$KEYVAULT_NAME" --name "weather-api-key" --value "PLACEHOLDER" -o none

echo "  Disabling public access..."
az keyvault update --name "$KEYVAULT_NAME" --resource-group "$RESOURCE_GROUP" --public-network-access Disabled -o none

# ---------------------------------------------------------------------------
# Step 2: Create Fabric Workspace
# ---------------------------------------------------------------------------
echo ""
echo "[4/7] Creating Fabric workspace..."
FABRIC_TOKEN=$(az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv)
FABRIC_HEADERS="Authorization: Bearer $FABRIC_TOKEN"

# Get capacity ID
CAPACITY_ID=$(curl -s -H "$FABRIC_HEADERS" "https://api.fabric.microsoft.com/v1/capacities" | \
  python3 -c "import sys,json; caps=json.load(sys.stdin)['value']; print(next(c['id'] for c in caps if c['displayName']=='$FABRIC_CAPACITY_NAME'))")

echo "  Capacity ID: $CAPACITY_ID"

# Create workspace
WORKSPACE_ID=$(curl -s -X POST \
  -H "$FABRIC_HEADERS" -H "Content-Type: application/json" \
  -d "{\"displayName\":\"$FABRIC_WORKSPACE_NAME\",\"capacityId\":\"$CAPACITY_ID\"}" \
  "https://api.fabric.microsoft.com/v1/workspaces" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "  Workspace ID: $WORKSPACE_ID"

# ---------------------------------------------------------------------------
# Step 3: Enable Workspace Identity & Grant KV Access
# ---------------------------------------------------------------------------
echo ""
echo "[5/7] Provisioning workspace identity..."
curl -s -X POST -H "$FABRIC_HEADERS" \
  "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/provisionIdentity" > /dev/null

sleep 10

# Get the workspace identity service principal ID
WS_IDENTITY_SP=$(curl -s -H "$FABRIC_HEADERS" \
  "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID" | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['workspaceIdentity']['servicePrincipalId'])")

echo "  Workspace identity SP: $WS_IDENTITY_SP"

echo "  Granting Key Vault Secrets User to workspace identity..."
az role assignment create \
  --assignee "$WS_IDENTITY_SP" \
  --role "Key Vault Secrets User" \
  --scope "$KV_RESOURCE_ID" \
  -o none

# ---------------------------------------------------------------------------
# Step 4: Create Lakehouse with Schemas
# ---------------------------------------------------------------------------
echo ""
echo "[6/7] Creating bronze lakehouse..."
FABRIC_TOKEN=$(az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv)
FABRIC_HEADERS="Authorization: Bearer $FABRIC_TOKEN"

LAKEHOUSE_ID=$(curl -s -X POST \
  -H "$FABRIC_HEADERS" -H "Content-Type: application/json" \
  -d '{"displayName":"bronze","type":"Lakehouse"}' \
  "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/items" | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

echo "  Lakehouse ID: $LAKEHOUSE_ID"
echo "  Waiting for SQL endpoint provisioning..."
sleep 30

# Get SQL endpoint connection string
SQL_CONN=$(curl -s -H "$FABRIC_HEADERS" \
  "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/lakehouses/$LAKEHOUSE_ID" | \
  python3 -c "import sys,json; print(json.load(sys.stdin)['properties']['sqlEndpointProperties']['connectionString'])")

echo "  SQL endpoint: $SQL_CONN"
echo "  Creating schemas (mbta, wmata, weather)..."

SQL_TOKEN=$(az account get-access-token --resource https://database.windows.net --query accessToken -o tsv)

python3 -c "
import pyodbc
conn = pyodbc.connect(
    f'DRIVER={{ODBC Driver 18 for SQL Server}};SERVER=$SQL_CONN;DATABASE=bronze;'
    f'AccessToken=$SQL_TOKEN;Encrypt=yes;TrustServerCertificate=no')
conn.autocommit = True
for schema in ['mbta', 'wmata', 'weather']:
    conn.execute(f'CREATE SCHEMA [{schema}]')
    print(f'  Created schema: {schema}')
conn.close()
" 2>/dev/null || echo "  (Schema creation via pyodbc failed — create manually in Fabric)"

# ---------------------------------------------------------------------------
# Step 5: Connect Workspace to Git
# ---------------------------------------------------------------------------
echo ""
echo "[7/7] Connecting workspace to GitHub..."
echo "  NOTE: You must have a GitHub connection configured in Fabric."
echo "  If you don't have one, create it in Fabric Settings > Manage connections"
echo "  before running this step."
echo ""
read -p "Enter your Fabric GitHub connection ID (or press Enter to skip git sync): " GIT_CONN_ID

if [ -n "$GIT_CONN_ID" ]; then
  FABRIC_TOKEN=$(az account get-access-token --resource https://api.fabric.microsoft.com --query accessToken -o tsv)
  FABRIC_HEADERS="Authorization: Bearer $FABRIC_TOKEN"

  curl -s -X POST \
    -H "$FABRIC_HEADERS" -H "Content-Type: application/json" \
    -d "{
      \"gitProviderDetails\": {
        \"gitProviderType\": \"GitHub\",
        \"ownerName\": \"$GITHUB_OWNER\",
        \"repositoryName\": \"$GITHUB_REPO\",
        \"branchName\": \"$GITHUB_BRANCH\",
        \"directoryName\": \"$GIT_DIRECTORY\"
      },
      \"myGitCredentials\": {
        \"source\": \"ConfiguredConnection\",
        \"connectionId\": \"$GIT_CONN_ID\"
      }
    }" \
    "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/git/connect" > /dev/null

  echo "  Git connected. Initializing sync..."
  curl -s -X POST \
    -H "$FABRIC_HEADERS" -H "Content-Type: application/json" \
    -d '{"initializationStrategy":"PreferRemote"}' \
    "https://api.fabric.microsoft.com/v1/workspaces/$WORKSPACE_ID/git/initializeConnection" > /dev/null

  echo "  Workspace synced from git."
else
  echo "  Skipped git integration — configure manually in Fabric."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=============================================="
echo " Setup Complete!"
echo "=============================================="
echo ""
echo " Azure:"
echo "   Resource Group:  $RESOURCE_GROUP"
echo "   Key Vault:       $KEYVAULT_NAME"
echo "   Key Vault URI:   https://$KEYVAULT_NAME.vault.azure.net/"
echo ""
echo " Fabric:"
echo "   Workspace:       $FABRIC_WORKSPACE_NAME ($WORKSPACE_ID)"
echo "   Lakehouse:       bronze ($LAKEHOUSE_ID)"
echo "   Schemas:         mbta, wmata, weather"
echo "   Identity SP:     $WS_IDENTITY_SP"
echo ""
echo " Next steps:"
echo "   1. If you skipped git sync, connect the workspace to your fork manually"
echo "   2. Update secret values in Key Vault if you used placeholders"
echo "   3. Update notebook Key Vault URL to: https://$KEYVAULT_NAME.vault.azure.net/"
echo "   4. Run nb_mbta_sse_ingest to start streaming data"
echo ""
