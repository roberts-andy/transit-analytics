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

# # MBTA Ingestion — Bronze Layer (Two-Tier: Batch + SSE)
# 
# **Tier 1 — Batch Reference Data:** Loads static/slow-changing endpoints via REST GET at startup,
# then refreshes periodically (hourly). These are large datasets that rarely change.
# 
# **Tier 2 — SSE Real-Time Streams:** Maintains persistent SSE connections for high-frequency
# endpoints (predictions, vehicles, alerts, live_facilities) that change every few seconds.
# 
# **Security:** API key is retrieved from Azure Key Vault at runtime.

# CELL ********************

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

# Configuration from Fabric Variable Library
config = notebookutils.variableLibrary.getLibrary("transit-analytics-config")
KEYVAULT_URL = config["keyvault_url"]
SECRET_MBTA_API_KEY = config["secret_mbta_api_key"]
MBTA_API_BASE = config["mbta_api_base"]
FLUSH_INTERVAL_SECONDS = int(config["mbta_sse_flush_interval_seconds"])
MAX_BACKOFF_SECONDS = int(config["mbta_sse_max_backoff_seconds"])
API_TIMEOUT = int(config["mbta_api_timeout_seconds"])

# Tier 1: Batch reference endpoints (loaded via REST, refreshed periodically)
BATCH_ENDPOINTS = {
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

# Tier 2: SSE real-time endpoints (persistent streaming connections)
SSE_ENDPOINTS = {
    "predictions":     "/predictions",
    "vehicles":        "/vehicles",
    "alerts":          "/alerts",
    "live_facilities": "/live_facilities",
}

# How often to refresh batch reference data (seconds)
BATCH_REFRESH_INTERVAL_SECONDS = 3600  # 1 hour

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Ensure Schema Exists

# CELL ********************

spark.sql("CREATE SCHEMA IF NOT EXISTS bronze.mbta")
print("Schema bronze.mbta ready")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Retrieve API Key from Key Vault

# CELL ********************

api_key = mssparkutils.credentials.getSecret(KEYVAULT_URL, SECRET_MBTA_API_KEY)
print(f"API key retrieved successfully ({len(api_key)} chars)")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Tier 1 — Batch Reference Data Loader
# 
# Fetches full snapshots from large, slow-changing endpoints via paginated REST calls.
# Each endpoint is overwritten as a Delta table on each refresh.

# CELL ********************

import requests
import json
import time
from datetime import datetime, timezone

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

def load_batch_endpoint(name: str, path: str, api_key: str):
    """Fetch an endpoint and overwrite its Delta table."""
    full_table = f"bronze.mbta.{name}"
    try:
        print(f"  Loading {name}...", end="")
        entities = fetch_all_pages(path, api_key)
        if not entities:
            print(f" 0 records (skipped)")
            return
        
        flat_rows = [flatten_entity(e) for e in entities]
        df = spark.createDataFrame(flat_rows)
        df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(full_table)
        print(f" {len(entities)} records → {full_table}")
    except Exception as e:
        print(f" ERROR: {e}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Initial Batch Load
# 
# Load all reference data before starting SSE streams.

# CELL ********************

print(f"[{datetime.now()}] Loading batch reference data...")
print("=" * 60)

for name, path in BATCH_ENDPOINTS.items():
    load_batch_endpoint(name, path, api_key)
    time.sleep(1)  # Respect rate limits

print("=" * 60)
print(f"[{datetime.now()}] Batch reference data loaded.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Tier 2 — SSE Client & Real-Time Streaming
# 
# A lightweight SSE parser that maintains persistent HTTP connections.
# Only used for high-frequency endpoints (predictions, vehicles, alerts, live_facilities).

# CELL ********************

import threading
import traceback
from collections import defaultdict
from pyspark.sql import functions as F
from delta.tables import DeltaTable

class SSEClient:
    """Lightweight SSE client that parses a streaming HTTP response into events."""
    
    def __init__(self, url: str, headers: dict, timeout: int = 90):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self._response = None
    
    def connect(self):
        self._response = requests.get(
            self.url,
            headers=self.headers,
            stream=True,
            timeout=(10, self.timeout)
        )
        self._response.raise_for_status()
        return self
    
    def events(self):
        """Yield parsed SSE events from the stream."""
        event_type = None
        data_lines = []
        
        for line in self._response.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if line == "":
                if event_type and data_lines:
                    data_str = "\n".join(data_lines)
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        data = data_str
                    yield {"event": event_type, "data": data}
                event_type = None
                data_lines = []
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
    
    def close(self):
        if self._response:
            self._response.close()


class EventBuffer:
    """Thread-safe buffer that collects SSE events and flushes them to Delta."""
    
    def __init__(self, spark_session):
        self.spark = spark_session
        self._lock = threading.Lock()
        self._buffers = defaultdict(lambda: {"adds": [], "updates": [], "removes": [], "reset": None})
        self._stats = defaultdict(lambda: {"adds": 0, "updates": 0, "removes": 0, "resets": 0, "flushes": 0, "errors": 0})
    
    def add_event(self, table_name: str, event_type: str, data):
        with self._lock:
            buf = self._buffers[table_name]
            if event_type == "reset":
                buf["reset"] = data if isinstance(data, list) else [data]
                buf["adds"] = []
                buf["updates"] = []
                buf["removes"] = []
                self._stats[table_name]["resets"] += 1
            elif event_type == "add":
                buf["adds"].append(data)
                self._stats[table_name]["adds"] += 1
            elif event_type == "update":
                buf["updates"].append(data)
                self._stats[table_name]["updates"] += 1
            elif event_type == "remove":
                buf["removes"].append(data)
                self._stats[table_name]["removes"] += 1
    
    def flush_all(self):
        with self._lock:
            tables_to_flush = dict(self._buffers)
            self._buffers = defaultdict(lambda: {"adds": [], "updates": [], "removes": [], "reset": None})
        
        for table_name, buf in tables_to_flush.items():
            try:
                self._flush_table(table_name, buf)
                self._stats[table_name]["flushes"] += 1
            except Exception as e:
                self._stats[table_name]["errors"] += 1
                print(f"[{datetime.now()}] ERROR flushing {table_name}: {e}")
                traceback.print_exc()
    
    def _flush_table(self, table_name: str, buf: dict):
        full_table = f"bronze.mbta.{table_name}"
        has_changes = buf["reset"] is not None or buf["adds"] or buf["updates"] or buf["removes"]
        if not has_changes:
            return
        
        if buf["reset"] is not None:
            df = self._entities_to_df(buf["reset"])
            if df is not None:
                df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(full_table)
                print(f"[{datetime.now()}] RESET {full_table}: {df.count()} entities")
            return
        
        upsert_entities = buf["adds"] + buf["updates"]
        remove_entities = buf["removes"]
        
        if upsert_entities:
            df = self._entities_to_df(upsert_entities)
            if df is not None:
                if not self.spark.catalog.tableExists(full_table):
                    df.write.format("delta").mode("overwrite").saveAsTable(full_table)
                    print(f"[{datetime.now()}] INIT {full_table}: {len(upsert_entities)} entities")
                    return
                
                delta_table = DeltaTable.forName(self.spark, full_table)
                delta_table.alias("target").merge(
                    df.alias("source"),
                    "target.id = source.id"
                ).whenMatchedUpdateAll(
                ).whenNotMatchedInsertAll(
                ).execute()
                print(f"[{datetime.now()}] MERGE {full_table}: +{len(buf['adds'])} ~{len(buf['updates'])}")
        
        if remove_entities:
            remove_ids = [e.get("id") for e in remove_entities if e.get("id")]
            if remove_ids:
                id_list = ",".join([f"'{rid}'" for rid in remove_ids])
                self.spark.sql(f"DELETE FROM {full_table} WHERE id IN ({id_list})")
                print(f"[{datetime.now()}] DELETE {full_table}: -{len(remove_ids)}")
    
    def _entities_to_df(self, entities: list):
        if not entities:
            return None
        flat_rows = [flatten_entity(e) for e in entities]
        return self.spark.createDataFrame(flat_rows)
    
    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## SSE Consumer Threads
# 
# Each real-time endpoint gets its own persistent SSE connection.
# Handles reconnection with exponential backoff and catches ChunkedEncodingError.

# CELL ********************

class SSEConsumerThread(threading.Thread):
    """Background thread that consumes SSE events from one MBTA endpoint."""
    
    def __init__(self, endpoint_name: str, path: str, api_key: str, event_buffer: EventBuffer):
        super().__init__(daemon=True, name=f"sse-{endpoint_name}")
        self.endpoint_name = endpoint_name
        self.path = path
        self.api_key = api_key
        self.event_buffer = event_buffer
        self._stop_event = threading.Event()
        self._connected = False
        self._backoff = 1
    
    def stop(self):
        self._stop_event.set()
    
    @property
    def is_connected(self):
        return self._connected
    
    def run(self):
        url = f"{MBTA_API_BASE}{self.path}"
        headers = {
            "Accept": "text/event-stream",
            "x-api-key": self.api_key,
        }
        
        while not self._stop_event.is_set():
            client = None
            try:
                print(f"[{datetime.now()}] Connecting to SSE: {self.endpoint_name}")
                client = SSEClient(url, headers)
                client.connect()
                self._connected = True
                self._backoff = 1
                print(f"[{datetime.now()}] Connected: {self.endpoint_name}")
                
                for event in client.events():
                    if self._stop_event.is_set():
                        break
                    self.event_buffer.add_event(self.endpoint_name, event["event"], event["data"])
                    
            except requests.exceptions.ChunkedEncodingError as e:
                print(f"[{datetime.now()}] ChunkedEncoding error on {self.endpoint_name}, reconnecting...")
            except requests.exceptions.Timeout:
                print(f"[{datetime.now()}] Timeout on {self.endpoint_name}, reconnecting...")
            except requests.exceptions.ConnectionError as e:
                print(f"[{datetime.now()}] Connection error on {self.endpoint_name}: {e}")
            except Exception as e:
                print(f"[{datetime.now()}] Error on {self.endpoint_name}: {e}")
                traceback.print_exc()
            finally:
                self._connected = False
                if client:
                    client.close()
            
            if not self._stop_event.is_set():
                sleep_time = min(self._backoff, MAX_BACKOFF_SECONDS)
                print(f"[{datetime.now()}] Reconnecting {self.endpoint_name} in {sleep_time}s...")
                self._stop_event.wait(sleep_time)
                self._backoff = min(self._backoff * 2, MAX_BACKOFF_SECONDS)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Orchestrator — Start SSE Streams & Periodic Flush + Batch Refresh
# 
# Launches SSE consumer threads for real-time endpoints, then runs the main loop that:
# 1. Flushes SSE event buffers to Delta every `FLUSH_INTERVAL_SECONDS`
# 2. Refreshes batch reference data every `BATCH_REFRESH_INTERVAL_SECONDS`

# CELL ********************

# Initialize the shared event buffer
event_buffer = EventBuffer(spark)

# Launch SSE consumer threads (only 4 real-time streams)
threads = {}
for name, path in SSE_ENDPOINTS.items():
    thread = SSEConsumerThread(name, path, api_key, event_buffer)
    thread.start()
    threads[name] = thread
    time.sleep(2)  # Stagger connections to avoid rate-limit bursts

print(f"\nStarted {len(threads)} SSE consumer threads: {list(threads.keys())}")
print(f"Flush interval: {FLUSH_INTERVAL_SECONDS}s | Batch refresh: {BATCH_REFRESH_INTERVAL_SECONDS}s")
print("=" * 60)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Main Loop — Flush, Monitor & Batch Refresh
# 
# Runs indefinitely. **To stop:** Interrupt the notebook or cancel the Spark session.

# CELL ********************

try:
    cycle = 0
    last_batch_refresh = time.time()
    
    while True:
        time.sleep(FLUSH_INTERVAL_SECONDS)
        cycle += 1
        
        # Flush SSE event buffers to Delta
        event_buffer.flush_all()
        
        # Refresh batch reference data periodically
        if time.time() - last_batch_refresh > BATCH_REFRESH_INTERVAL_SECONDS:
            print(f"\n[{datetime.now()}] Refreshing batch reference data...")
            for name, path in BATCH_ENDPOINTS.items():
                load_batch_endpoint(name, path, api_key)
                time.sleep(1)
            last_batch_refresh = time.time()
            print(f"[{datetime.now()}] Batch refresh complete.")
        
        # Print status every 5 cycles
        if cycle % 5 == 0:
            stats = event_buffer.get_stats()
            connected = sum(1 for t in threads.values() if t.is_connected)
            print(f"\n[{datetime.now()}] STATUS: {connected}/{len(threads)} SSE streams connected")
            for table, s in sorted(stats.items()):
                print(f"  {table:20s} | adds:{s['adds']:6d} | updates:{s['updates']:6d} | "
                      f"removes:{s['removes']:6d} | resets:{s['resets']:4d} | "
                      f"flushes:{s['flushes']:4d} | errors:{s['errors']:3d}")
            print()

except KeyboardInterrupt:
    print(f"\n[{datetime.now()}] Shutting down gracefully...")
finally:
    for name, thread in threads.items():
        thread.stop()
    
    print("Final flush...")
    event_buffer.flush_all()
    
    for name, thread in threads.items():
        thread.join(timeout=10)
    
    print(f"[{datetime.now()}] All streams stopped.")
    stats = event_buffer.get_stats()
    print("\nFinal ingestion stats:")
    for table, s in sorted(stats.items()):
        total = s['adds'] + s['updates'] + s['removes']
        print(f"  {table:20s} | total events: {total:8d} | resets: {s['resets']:4d} | errors: {s['errors']:3d}")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
