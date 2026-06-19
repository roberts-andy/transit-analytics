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

# # MBTA Stop Events — Daily Batch Ingestion
# 
# Pulls actual arrival/departure data from the `/stop_events` endpoint for the previous day.
# This endpoint requires filters, so we query by route for all active routes.
# 
# **Schedule:** Daily (run after service day completes, e.g., 06:00 ET)
# 
# **Target table:** `bronze.mbta.stop_events` (partitioned by `start_date`)

# CELL ********************

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import *
from delta.tables import DeltaTable
from datetime import datetime, timedelta, timezone
import requests
import json
import time

spark = SparkSession.builder.getOrCreate()

# Configuration from Fabric Variable Library (transit-analytics-config)
config = notebookutils.variableLibrary.getLibrary("transit-analytics-config")
KEYVAULT_URL = config["keyvault_url"]
SECRET_MBTA_API_KEY = config["secret_mbta_api_key"]
MBTA_API_BASE = config["mbta_api_base"]
MBTA_API_TIMEOUT_SECONDS = int(config["mbta_api_timeout_seconds"])

TABLE_NAME = "bronze.mbta.stop_events"

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

# ## Determine Target Date & Active Routes

# CELL ********************

# Default: yesterday's service date. Override via notebook parameter if needed.
target_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
print(f"Target service date: {target_date}")

# Get all active routes to iterate over
headers = {"x-api-key": api_key, "Accept": "application/vnd.api+json"}
response = requests.get(f"{MBTA_API_BASE}/routes", headers=headers, timeout=MBTA_API_TIMEOUT_SECONDS)
response.raise_for_status()
routes_data = response.json()["data"]
route_ids = [r["id"] for r in routes_data]
print(f"Found {len(route_ids)} routes to query")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Fetch Stop Events for All Routes
# 
# Paginates through all stop events for each route on the target date.
# Includes rate-limit awareness to avoid 429 responses.

# CELL ********************

def fetch_stop_events_for_route(route_id: str, date: str) -> list:
    """Fetch all stop events for a route on a given date, handling pagination."""
    all_events = []
    url = f"{MBTA_API_BASE}/stop_events"
    params = {
        "filter[route]": route_id,
        "page[limit]": 100,
        "page[offset]": 0,
    }
    
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=MBTA_API_TIMEOUT_SECONDS)
        
        if response.status_code == 429:
            # Rate limited — wait and retry
            retry_after = int(response.headers.get("Retry-After", 10))
            print(f"  Rate limited on route {route_id}, waiting {retry_after}s...")
            time.sleep(retry_after)
            continue
        
        response.raise_for_status()
        data = response.json()
        all_events.extend(data.get("data", []))
        
        # Follow pagination
        next_link = data.get("links", {}).get("next")
        if next_link:
            url = next_link
            params = None  # params are embedded in next_link
        else:
            break
    
    return all_events

# Collect stop events across all routes
all_stop_events = []
errors = []

for i, route_id in enumerate(route_ids):
    try:
        events = fetch_stop_events_for_route(route_id, target_date)
        all_stop_events.extend(events)
        if events:
            print(f"  [{i+1}/{len(route_ids)}] Route {route_id}: {len(events)} events")
    except Exception as e:
        errors.append({"route_id": route_id, "error": str(e)})
        print(f"  [{i+1}/{len(route_ids)}] Route {route_id}: ERROR - {e}")
    
    # Small delay between routes to be respectful of rate limits
    time.sleep(0.2)

print(f"\nTotal stop events collected: {len(all_stop_events)}")
if errors:
    print(f"Errors on {len(errors)} routes: {errors}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Flatten & Write to Delta
# 
# Flattens the JSON:API response and writes/merges into the stop_events Delta table,
# partitioned by `start_date` for efficient querying.

# CELL ********************

if not all_stop_events:
    print("No stop events found for target date. Exiting.")
    mssparkutils.notebook.exit("no_data")

# Flatten JSON:API entities
def flatten_stop_event(entity: dict) -> dict:
    attrs = entity.get("attributes", {})
    rels = entity.get("relationships", {})
    
    return {
        "id": entity.get("id"),
        "start_date": attrs.get("start_date"),
        "trip_id": attrs.get("trip_id"),
        "route_id": attrs.get("route_id"),
        "vehicle_id": attrs.get("vehicle_id"),
        "stop_id": attrs.get("stop_id"),
        "stop_sequence": attrs.get("stop_sequence"),
        "direction_id": attrs.get("direction_id"),
        "arrived": attrs.get("arrived"),
        "departed": attrs.get("departed"),
        "revenue": attrs.get("revenue"),
        "rel_trip_id": rels.get("trip", {}).get("data", {}).get("id") if rels.get("trip", {}).get("data") else None,
        "rel_stop_id": rels.get("stop", {}).get("data", {}).get("id") if rels.get("stop", {}).get("data") else None,
        "rel_route_id": rels.get("route", {}).get("data", {}).get("id") if rels.get("route", {}).get("data") else None,
        "rel_vehicle_id": rels.get("vehicle", {}).get("data", {}).get("id") if rels.get("vehicle", {}).get("data") else None,
        "_ingested_at": datetime.now(timezone.utc).isoformat(),
        "_service_date": target_date,
    }

flat_events = [flatten_stop_event(e) for e in all_stop_events]
df = spark.createDataFrame(flat_events)

# Deduplicate by composite key (same event can appear from multiple route queries)
df = df.dropDuplicates(["id"])

print(f"DataFrame ready: {df.count()} unique stop events")
df.printSchema()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

# Write to Delta — MERGE by ID to handle re-runs gracefully
if not spark.catalog.tableExists(TABLE_NAME):
    print(f"Creating table {TABLE_NAME} (partitioned by _service_date)")
    df.write.format("delta") \
        .partitionBy("_service_date") \
        .mode("overwrite") \
        .saveAsTable(TABLE_NAME)
else:
    delta_table = DeltaTable.forName(spark, TABLE_NAME)
    delta_table.alias("target").merge(
        df.alias("source"),
        "target.id = source.id"
    ).whenMatchedUpdateAll(
    ).whenNotMatchedInsertAll(
    ).execute()

print(f"✓ Stop events for {target_date} written to {TABLE_NAME}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
