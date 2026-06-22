# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {
# META     "name": "synapse_pyspark"
# META   },
# META   "dependencies": {
# META     "lakehouse": {
# META       "default_lakehouse": "82a4dd04-28a2-4ac4-acab-254953df7edb",
# META       "default_lakehouse_name": "bronze",
# META       "default_lakehouse_workspace_id": "c030c477-6e50-4334-8fcb-fd032f8870b9",
# META       "known_lakehouses": [
# META         {
# META           "id": "82a4dd04-28a2-4ac4-acab-254953df7edb"
# META         }
# META       ]
# META     }
# META   }
# META }

# MARKDOWN ********************

# # MBTA Reference Data — Daily Incremental Load
# 
# Fetches full snapshots from slow-changing MBTA endpoints, then uses Delta MERGE
# to detect and apply only actual changes. Each row carries metadata:
# 
# - `_row_hash` — SHA-256 of all data columns (change detection)
# - `_created_at` — When the row was first ingested
# - `_updated_at` — When the row was last modified
# - `_is_active` — `true` if still present in source; `false` if soft-deleted
# 
# **Schedule:** Daily via `pl_mbta_reference_daily`

# CELL ********************

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType
from delta.tables import DeltaTable
from datetime import datetime, timezone
import requests
import json
import time
import hashlib

spark = SparkSession.builder.getOrCreate()

# Ensure schema exists
spark.sql("CREATE SCHEMA IF NOT EXISTS mbta")

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

api_key = notebookutils.credentials.getSecret(KEYVAULT_URL, SECRET_MBTA_API_KEY)
print(f"API key retrieved ({len(api_key)} chars)")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Helper Functions

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
    """Flatten a JSON:API entity into a flat dict (all values as strings)."""
    flat = {"id": str(entity.get("id", "")), "type": str(entity.get("type", ""))}
    
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


def compute_row_hash(row_dict: dict) -> str:
    """SHA-256 hash of all data columns for change detection."""
    content = "|".join(f"{k}={v}" for k, v in sorted(row_dict.items()))
    return hashlib.sha256(content.encode()).hexdigest()

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Incremental MERGE Logic
# 
# For each endpoint:
# 1. Fetch all records from API
# 2. Compute `_row_hash` for change detection
# 3. MERGE into Delta: insert new, update changed (hash differs), skip unchanged
# 4. Soft-delete rows no longer present in source (`_is_active = false`)

# CELL ********************

def load_endpoint_incremental(name: str, path: str, api_key: str, now_utc: str) -> dict:
    """Fetch endpoint data and MERGE into Delta — only touches changed rows."""
    full_table = f"mbta.{name}"
    stats = {"name": name, "fetched": 0, "inserted": 0, "updated": 0, "soft_deleted": 0}
    
    print(f"  {name}...", end="")
    entities = fetch_all_pages(path, api_key)
    stats["fetched"] = len(entities)
    
    if not entities:
        print(f" 0 records (skipped)")
        return stats
    
    # Flatten entities and compute content hash
    flat_rows = []
    for e in entities:
        flat = flatten_entity(e)
        flat["_row_hash"] = compute_row_hash(flat)
        flat_rows.append(flat)
    
    # Build unified all-string schema across all rows
    all_fields = set()
    for row in flat_rows:
        all_fields.update(row.keys())
    all_fields.update(["_created_at", "_updated_at", "_is_active"])
    field_names = sorted(all_fields)
    schema = StructType([StructField(f, StringType(), True) for f in field_names])
    
    # Normalize rows
    normalized = []
    for row in flat_rows:
        norm = {f: row.get(f, "") for f in field_names}
        norm["_created_at"] = now_utc
        norm["_updated_at"] = now_utc
        norm["_is_active"] = "true"
        normalized.append(norm)
    
    df_source = spark.createDataFrame(normalized, schema=schema)
    
    # First run — create table directly
    if not spark.catalog.tableExists(full_table):
        df_source.write.format("delta").mode("overwrite").saveAsTable(full_table)
        stats["inserted"] = len(flat_rows)
        print(f" {len(flat_rows)} records (new table created)")
        return stats
    
    # MERGE: update only rows where _row_hash changed
    delta_table = DeltaTable.forName(spark, full_table)
    
    # Build column maps for update (preserve _created_at from target)
    update_cols = {c: F.col(f"source.{c}") for c in field_names if c != "_created_at"}
    
    delta_table.alias("target").merge(
        df_source.alias("source"),
        "target.id = source.id"
    ).whenMatchedUpdate(
        condition="target._row_hash != source._row_hash",
        set=update_cols
    ).whenNotMatchedInsert(
        values={c: F.col(f"source.{c}") for c in field_names}
    ).execute()
    
    # Soft-delete: rows in target that are active but not in source
    source_ids = df_source.select("id")
    stale_rows = (
        delta_table.toDF().alias("t")
        .join(source_ids.alias("s"), F.col("t.id") == F.col("s.id"), "left_anti")
        .filter(F.col("t._is_active") == "true")
    )
    
    soft_delete_count = stale_rows.count()
    if soft_delete_count > 0:
        stale_ids = [row["id"] for row in stale_rows.select("id").collect()]
        # Batch the soft-delete in chunks to avoid SQL length limits
        for i in range(0, len(stale_ids), 500):
            chunk = stale_ids[i:i+500]
            id_list = ",".join([f"'{sid}'" for sid in chunk])
            spark.sql(f"""
                UPDATE {full_table} 
                SET _is_active = 'false', _updated_at = '{now_utc}' 
                WHERE id IN ({id_list})
            """)
        stats["soft_deleted"] = soft_delete_count
    
    # Count active records
    active_count = delta_table.toDF().filter(F.col("_is_active") == "true").count()
    print(f" {stats['fetched']} fetched | {active_count} active | {soft_delete_count} soft-deleted")
    return stats

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Execute Load

# CELL ********************

now_utc = datetime.now(timezone.utc).isoformat()
print(f"[{datetime.now()}] MBTA reference data — incremental merge")
print(f"Timestamp: {now_utc}")
print("=" * 60)

all_stats = []
errors = []

for name, path in ENDPOINTS.items():
    try:
        stats = load_endpoint_incremental(name, path, api_key, now_utc)
        all_stats.append(stats)
    except Exception as e:
        errors.append({"endpoint": name, "error": str(e)})
        print(f" ERROR: {e}")
    time.sleep(1)

print("=" * 60)
total_fetched = sum(s["fetched"] for s in all_stats)
total_deleted = sum(s["soft_deleted"] for s in all_stats)
print(f"[{datetime.now()}] Complete: {total_fetched} records across {len(all_stats)} endpoints")
if total_deleted:
    print(f"  Soft-deleted: {total_deleted} stale records")
if errors:
    print(f"\nERRORS ({len(errors)}):")
    for err in errors:
        print(f"  {err['endpoint']}: {err['error']}")
    raise RuntimeError(f"Failed: {[e['endpoint'] for e in errors]}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
