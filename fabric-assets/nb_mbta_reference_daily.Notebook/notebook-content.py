# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse_name": "bronze",
# META       "default_lakehouse_workspace_id": "c030c477-6e50-4334-8fcb-fd032f8870b9",
# META       "known_lakehouses": [
# META         {
# META           "id": "e7c516ba-df49-4dc6-9f32-299392d999c9"
# META         }
# META       ]
# META     }
# META   }
# META }

# MARKDOWN ********************

# # MBTA Reference Data — Daily Batch Ingestion
# 
# Fetches full snapshots of all static/slow-changing MBTA endpoints via paginated REST.
# Each endpoint is overwritten as a Delta table in `bronze.mbta.*`.
# 
# **Endpoints:** routes, stops, lines, shapes, route_patterns, facilities, services, schedules, trips
# 
# **Schedule:** Daily (these change at most with GTFS service changes)

# CELL ********************

from pyspark.sql import SparkSession
from datetime import datetime, timezone
import requests
import json
import time

spark = SparkSession.builder.getOrCreate()

# Ensure schema exists
spark.sql("CREATE SCHEMA IF NOT EXISTS bronze.mbta")

# Configuration from Fabric Variable Library
config = notebookutils.variableLibrary.getLibrary("transit-analytics-config")
KEYVAULT_URL = config["keyvault_url"]
SECRET_MBTA_API_KEY = config["secret_mbta_api_key"]
MBTA_API_BASE = config["mbta_api_base"]
API_TIMEOUT = int(config["mbta_api_timeout_seconds"])

# Reference endpoints to load
ENDPOINTS = {
    "routes":          "/routes",
    "stops":           "/stops",
    "lines":           "/lines",
    "shapes":          "/shapes",
    "route_patterns":  "/route_patterns",
    "facilities":      "/facilities",
    "services":        "/services",
    "schedules":       "/schedules",
    "trips":           "/trips",
}

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Retrieve API Key

# CELL ********************

api_key = mssparkutils.credentials.getSecret(KEYVAULT_URL, SECRET_MBTA_API_KEY)
print(f"API key retrieved ({len(api_key)} chars)")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Fetch & Load All Reference Endpoints

# CELL ********************

def fetch_all_pages(endpoint_path: str, api_key: str) -> list:
    """Fetch all pages from a JSON:API endpoint."""
    url = f"{MBTA_API_BASE}{endpoint_path}"
    headers = {"x-api-key": api_key, "Accept": "application/vnd.api+json"}
    all_data = []
    
    while url:
        response = requests.get(url, headers=headers, timeout=API_TIMEOUT)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 10))
            print(f"  Rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        body = response.json()
        all_data.extend(body.get("data", []))
        url = body.get("links", {}).get("next")
    
    return all_data

def flatten_entity(entity: dict) -> dict:
    """Flatten a JSON:API entity into a flat dict for Delta storage."""
    flat = {"id": entity.get("id"), "type": entity.get("type")}
    
    for key, value in entity.get("attributes", {}).items():
        if isinstance(value, (dict, list)):
            flat[f"attr_{key}"] = json.dumps(value)
        else:
            flat[f"attr_{key}"] = value
    
    for rel_name, rel_data in entity.get("relationships", {}).items():
        rel_inner = rel_data.get("data")
        if isinstance(rel_inner, dict):
            flat[f"rel_{rel_name}_id"] = rel_inner.get("id")
            flat[f"rel_{rel_name}_type"] = rel_inner.get("type")
        elif isinstance(rel_inner, list):
            flat[f"rel_{rel_name}_ids"] = json.dumps([r.get("id") for r in rel_inner])
    
    flat["_ingested_at"] = datetime.now(timezone.utc).isoformat()
    return flat

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

print(f"[{datetime.now()}] Loading MBTA reference data...")
print("=" * 60)

results = {}
errors = []

for name, path in ENDPOINTS.items():
    full_table = f"bronze.mbta.{name}"
    try:
        print(f"  {name}...", end="")
        entities = fetch_all_pages(path, api_key)
        if not entities:
            print(f" 0 records (skipped)")
            results[name] = 0
            continue
        
        flat_rows = [flatten_entity(e) for e in entities]
        df = spark.createDataFrame(flat_rows)
        df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(full_table)
        results[name] = len(entities)
        print(f" {len(entities)} records → {full_table}")
    except Exception as e:
        errors.append({"endpoint": name, "error": str(e)})
        print(f" ERROR: {e}")
    
    time.sleep(1)  # Respect rate limits between endpoints

print("=" * 60)
print(f"[{datetime.now()}] Reference data load complete.")
print(f"\nSummary: {sum(results.values())} total records across {len(results)} tables")
if errors:
    print(f"ERRORS ({len(errors)}):")
    for err in errors:
        print(f"  {err['endpoint']}: {err['error']}")
    raise RuntimeError(f"Failed to load {len(errors)} endpoint(s): {[e['endpoint'] for e in errors]}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
