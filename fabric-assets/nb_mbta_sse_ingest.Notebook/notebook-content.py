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

# # MBTA Real-Time SSE Ingestion — Bronze Layer
# 
# Maintains persistent Server-Sent Event connections to high-frequency MBTA endpoints
# and writes changes to `mbta.*` Delta tables in the bronze lakehouse.
# 
# **Streams:** predictions, vehicles, alerts, live_facilities
# 
# **Event types:**
# - `reset` — Full snapshot (replaces table)
# - `add` / `update` — Upsert by ID
# - `remove` — Delete by ID
# 
# **Reference data** (routes, stops, etc.) is handled by `nb_mbta_reference_daily`.

# CELL ********************

from pyspark.sql import SparkSession
from pyspark.sql.types import StructType, StructField, StringType
from delta.tables import DeltaTable
import requests
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from collections import defaultdict

spark = SparkSession.builder.getOrCreate()

# Configuration from Fabric Variable Library
config = notebookutils.variableLibrary.getLibrary("transit-analytics-config")
KEYVAULT_URL = config["keyvault_url"]
SECRET_MBTA_API_KEY = config["secret_mbta_api_key"]
MBTA_API_BASE = config["mbta_api_base"]
FLUSH_INTERVAL_SECONDS = int(config["mbta_sse_flush_interval_seconds"])
MAX_BACKOFF_SECONDS = int(config["mbta_sse_max_backoff_seconds"])

# Real-time SSE endpoints (4 streams)
SSE_ENDPOINTS = {
    # "predictions":     "/predictions",
    "vehicles":        "/vehicles",
    "alerts":          "/alerts",
    # "live_facilities": "/live_facilities",
}

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Ensure Schema & Retrieve API Key

# CELL ********************

print("Schema mbta ready")

api_key = notebookutils.credentials.getSecret(KEYVAULT_URL, SECRET_MBTA_API_KEY)
print(f"API key retrieved ({len(api_key)} chars)")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## SSE Client

# CELL ********************

class SSEClient:
    """Lightweight SSE client that parses a streaming HTTP response into events."""
    
    def __init__(self, url: str, headers: dict, timeout: int = 90):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self._response = None
    
    def connect(self):
        self._response = requests.get(
            self.url, headers=self.headers, stream=True,
            timeout=(10, self.timeout)
        )
        self._response.raise_for_status()
        return self
    
    def events(self):
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

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Event Buffer & Delta Writer

# CELL ********************

def flatten_entity(entity: dict) -> dict:
    """Flatten a JSON:API entity to a flat dict. All values stored as strings."""
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
    
    flat["_ingested_at"] = datetime.now(timezone.utc).isoformat()
    return flat


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
    
    def _entities_to_df(self, entities: list):
        """Convert entities to a DataFrame with all-string schema for safety."""
        if not entities:
            return None
        flat_rows = [flatten_entity(e) for e in entities]
        # Collect all field names across all rows
        all_fields = set()
        for row in flat_rows:
            all_fields.update(row.keys())
        field_names = sorted(all_fields)
        schema = StructType([StructField(name, StringType(), True) for name in field_names])
        normalized = [{name: row.get(name, "") for name in field_names} for row in flat_rows]
        return self.spark.createDataFrame(normalized, schema=schema)
    
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
        full_table = f"mbta.{table_name}"
        has_changes = buf["reset"] is not None or buf["adds"] or buf["updates"] or buf["removes"]
        if not has_changes:
            return
        
        # RESET — full table replacement
        if buf["reset"] is not None:
            df = self._entities_to_df(buf["reset"])
            if df is not None:
                df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(full_table)
                print(f"[{datetime.now()}] RESET {full_table}: {df.count()} entities")
            return
        
        # Incremental upserts
        upsert_entities = buf["adds"] + buf["updates"]
        if upsert_entities:
            df = self._entities_to_df(upsert_entities)
            if df is not None:
                if not self.spark.catalog.tableExists(full_table):
                    df.write.format("delta").mode("overwrite").saveAsTable(full_table)
                    print(f"[{datetime.now()}] INIT {full_table}: {len(upsert_entities)} entities")
                else:
                    delta_table = DeltaTable.forName(self.spark, full_table)
                    delta_table.alias("target").merge(
                        df.alias("source"), "target.id = source.id"
                    ).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()
                    print(f"[{datetime.now()}] MERGE {full_table}: +{len(buf['adds'])} ~{len(buf['updates'])}")
        
        # Removes
        remove_entities = buf["removes"]
        if remove_entities:
            remove_ids = [e.get("id") for e in remove_entities if e.get("id")]
            if remove_ids:
                id_list = ",".join([f"'{rid}'" for rid in remove_ids])
                self.spark.sql(f"DELETE FROM {full_table} WHERE id IN ({id_list})")
                print(f"[{datetime.now()}] DELETE {full_table}: -{len(remove_ids)}")
    
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

# CELL ********************

class SSEConsumerThread(threading.Thread):
    """Background thread consuming SSE events from one MBTA endpoint."""
    
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
        headers = {"Accept": "text/event-stream", "x-api-key": self.api_key}
        
        while not self._stop_event.is_set():
            client = None
            try:
                print(f"[{datetime.now()}] Connecting: {self.endpoint_name}")
                client = SSEClient(url, headers)
                client.connect()
                self._connected = True
                self._backoff = 1
                print(f"[{datetime.now()}] Connected: {self.endpoint_name}")
                
                for event in client.events():
                    if self._stop_event.is_set():
                        break
                    self.event_buffer.add_event(self.endpoint_name, event["event"], event["data"])
                    
            except requests.exceptions.ChunkedEncodingError:
                print(f"[{datetime.now()}] Chunked encoding error on {self.endpoint_name}, reconnecting...")
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

# ## Start Streams & Main Loop

# CELL ********************

event_buffer = EventBuffer(spark)

threads = {}
for name, path in SSE_ENDPOINTS.items():
    thread = SSEConsumerThread(name, path, api_key, event_buffer)
    thread.start()
    threads[name] = thread
    time.sleep(2)

print(f"\nStarted {len(threads)} SSE streams: {list(threads.keys())}")
print(f"Flush interval: {FLUSH_INTERVAL_SECONDS}s")
print("=" * 60)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# CELL ********************

try:
    cycle = 0
    while True:
        time.sleep(FLUSH_INTERVAL_SECONDS)
        cycle += 1
        event_buffer.flush_all()
        
        # Status every 5 cycles (~2.5 min)
        if cycle % 5 == 0:
            stats = event_buffer.get_stats()
            connected = sum(1 for t in threads.values() if t.is_connected)
            print(f"\n[{datetime.now()}] STATUS: {connected}/{len(threads)} connected")
            for table, s in sorted(stats.items()):
                print(f"  {table:20s} | adds:{s['adds']:6d} | updates:{s['updates']:6d} | "
                      f"removes:{s['removes']:6d} | resets:{s['resets']:4d} | errors:{s['errors']:3d}")
            print()

except KeyboardInterrupt:
    print(f"\n[{datetime.now()}] Shutting down...")
finally:
    for thread in threads.values():
        thread.stop()
    print("Final flush...")
    event_buffer.flush_all()
    for thread in threads.values():
        thread.join(timeout=10)
    print(f"[{datetime.now()}] Done.")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }
