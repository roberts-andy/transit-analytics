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
            flat[f"attr_{key}"] = "" if value is None else str(value)
    
    for rel_name, rel_data in entity.get("relationships", {}).items():
        rel_inner = rel_data.get("data")
        if isinstance(rel_inner, dict):
            flat[f"rel_{rel_name}_id"] = str(rel_inner.get("id", ""))
            flat[f"rel_{rel_name}_type"] = str(rel_inner.get("type", ""))
        elif isinstance(rel_inner, list):
            flat[f"rel_{rel_name}_ids"] = json.dumps([r.get("id") for r in rel_inner])
    
    return flat

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType
from delta.tables import DeltaTable
import hashlib

def compute_row_hash(row_dict: dict) -> str:
    """Compute a SHA-256 hash of all data columns (excludes metadata columns)."""
    # Sort keys for deterministic hashing
    content = "|".join(f"{k}={v}" for k, v in sorted(row_dict.items()))
    return hashlib.sha256(content.encode()).hexdigest()

def load_endpoint_with_merge(name: str, path: str, api_key: str):
    """Fetch an endpoint and MERGE into Delta — only update rows that changed."""
    full_table = f"mbta.{name}"
    now_utc = datetime.now(timezone.utc).isoformat()
    
    try:
        print(f"  {name}...", end="")
        entities = fetch_all_pages(path, api_key)
        if not entities:
            print(f" 0 records (skipped)")
            return {"name": name, "fetched": 0, "inserted": 0, "updated": 0, "deleted": 0}
        
        # Flatten and add content hash
        flat_rows = []
        for e in entities:
            flat = flatten_entity(e)
            flat["_row_hash"] = compute_row_hash(flat)
            flat_rows.append(flat)
        
        # Build all-string schema for consistency
        all_fields = set()
        for row in flat_rows:
            all_fields.update(row.keys())
        # Add metadata columns
        all_fields.update(["_created_at", "_updated_at", "_is_active"])
        field_names = sorted(all_fields)
        schema = StructType([StructField(f, StringType(), True) for f in field_names])
        
        # Normalize rows (fill missing fields)
        normalized = []
        for row in flat_rows:
            norm = {f: row.get(f, "") for f in field_names}
            norm["_created_at"] = now_utc  # Will be overridden by merge logic for existing rows
            norm["_updated_at"] = now_utc
            norm["_is_active"] = "true"
            normalized.append(norm)
        
        df_source = spark.createDataFrame(normalized, schema=schema)
        
        # If table doesn't exist, create it
        if not spark.catalog.tableExists(full_table):
            df_source.write.format("delta").mode("overwrite").saveAsTable(full_table)
            count = len(flat_rows)
            print(f" {count} records (new table)")
            return {"name": name, "fetched": count, "inserted": count, "updated": 0, "deleted": 0}
        
        # MERGE: match on id, update only when hash differs, insert new, soft-delete removed
        delta_table = DeltaTable.forName(spark, full_table)
        
        # Columns to update (everything except _created_at)
        update_cols = {c: f"source.{c}" for c in field_names if c not in ("_created_at",)}
        insert_cols = {c: f"source.{c}" for c in field_names}
        
        merge_result = delta_table.alias("target").merge(
            df_source.alias("source"),
            "target.id = source.id"
        ).whenMatchedUpdate(
            condition="target._row_hash != source._row_hash",
            set=update_cols
        ).whenNotMatchedInsertAll(
        ).execute()
        
        # Soft-delete: mark rows no longer in source as inactive
        source_ids = df_source.select("id")
        inactive_df = delta_table.toDF().alias("t").join(
            source_ids.alias("s"), F.col("t.id") == F.col("s.id"), "left_anti"
        ).filter(F.col("t._is_active") == "true")
        
        deleted_count = inactive_df.count()
        if deleted_count > 0:
            inactive_ids = [row.id for row in inactive_df.select("t.id").collect()]
            id_list = ",".join([f"'{i}'" for i in inactive_ids])
            spark.sql(f"UPDATE {full_table} SET _is_active = 'false', _updated_at = '{now_utc}' WHERE id IN ({id_list})")
        
        # Get stats from the merge (approximate via count comparison)
        new_total = delta_table.toDF().filter(F.col("_is_active") == "true").count()
        fetched = len(flat_rows)
        print(f" {fetched} fetched | active: {new_total} | soft-deleted: {deleted_count}")
        return {"name": name, "fetched": fetched, "inserted": 0, "updated": 0, "deleted": deleted_count}
        
    except Exception as e:
        print(f" ERROR: {e}")
        raise

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Execute MERGE for All Reference Endpoints

# CELL ********************

print(f"[{datetime.now()}] Loading MBTA reference data (incremental merge)...")
print("=" * 60)

results = []
errors = []

for name, path in ENDPOINTS.items():
    try:
        result = load_endpoint_with_merge(name, path, api_key)
        results.append(result)
    except Exception as e:
        errors.append({"endpoint": name, "error": str(e)})
    time.sleep(1)

print("=" * 60)
print(f"[{datetime.now()}] Reference data load complete.")
total_fetched = sum(r["fetched"] for r in results)
print(f"\nSummary: {total_fetched} total records fetched across {len(results)} endpoints")
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
