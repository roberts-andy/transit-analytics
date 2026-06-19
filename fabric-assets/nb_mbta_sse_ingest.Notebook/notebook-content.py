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

# # MBTA SSE Ingestion — Bronze Layer
# 
# Streams all MBTA V3 API entities via Server-Sent Events into `bronze.mbta.*` Delta tables.
# 
# **Event types from the MBTA SSE stream:**
# - `reset` — Full dump of all entities (replaces current state)
# - `add` — New entity added
# - `update` — Existing entity modified  
# - `remove` — Entity removed
# 
# **Security:** API key is retrieved from Azure Key Vault at runtime — never stored in code.

# CELL ********************

# Configuration — Endpoints and table mappings
KEYVAULT_URL = "https://kvtransitdemo-f70cfb6a.vault.azure.net/"
KEYVAULT_SECRET_NAME = "mbta-api-key"
MBTA_API_BASE = "https://api-v3.mbta.com"

# All MBTA V3 API SSE endpoints and their target Delta table names
ENDPOINTS = {
    "routes":         {"path": "/routes",         "table": "routes",         "id_field": "id"},
    "stops":          {"path": "/stops",          "table": "stops",          "id_field": "id"},
    "lines":          {"path": "/lines",          "table": "lines",          "id_field": "id"},
    "shapes":         {"path": "/shapes",         "table": "shapes",         "id_field": "id"},
    "route_patterns": {"path": "/route_patterns", "table": "route_patterns", "id_field": "id"},
    "facilities":     {"path": "/facilities",     "table": "facilities",     "id_field": "id"},
    "services":       {"path": "/services",       "table": "services",       "id_field": "id"},
    "schedules":      {"path": "/schedules",      "table": "schedules",      "id_field": "id"},
    "predictions":    {"path": "/predictions",    "table": "predictions",    "id_field": "id"},
    "vehicles":       {"path": "/vehicles",       "table": "vehicles",       "id_field": "id"},
    "alerts":         {"path": "/alerts",         "table": "alerts",         "id_field": "id"},
    "trips":          {"path": "/trips",          "table": "trips",          "id_field": "id"},
    "live_facilities": {"path": "/live_facilities", "table": "live_facilities", "id_field": "id"},
}

# How often to flush buffered events to Delta (seconds)
FLUSH_INTERVAL_SECONDS = 30

# Max reconnect backoff (seconds)
MAX_BACKOFF_SECONDS = 300

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Retrieve API Key from Key Vault

# CELL ********************

api_key = mssparkutils.credentials.getSecret(KEYVAULT_URL, KEYVAULT_SECRET_NAME)
print(f"API key retrieved successfully ({len(api_key)} chars)")

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## SSE Client & Event Processing Engine
# 
# A lightweight SSE parser that maintains a persistent HTTP connection.
# The MBTA API sends events as:
# ```
# event: reset
# data: [{"id": "...", "type": "...", "attributes": {...}, "relationships": {...}}, ...]
# 
# event: add
# data: {"id": "...", "type": "...", "attributes": {...}, "relationships": {...}}
# 
# event: update
# data: {"id": "...", "type": "...", "attributes": {...}, "relationships": {...}}
# 
# event: remove
# data: {"id": "...", "type": ".."}
# ```

# CELL ********************

import requests
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from collections import defaultdict
from queue import Queue, Empty

class SSEClient:
    """Lightweight SSE client that parses a streaming HTTP response into events."""
    
    def __init__(self, url: str, headers: dict, timeout: int = 90):
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self._response = None
    
    def connect(self):
        """Open a persistent streaming connection."""
        self._response = requests.get(
            self.url,
            headers=self.headers,
            stream=True,
            timeout=(10, self.timeout)  # (connect_timeout, read_timeout)
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
                # Empty line = end of event
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
            # Ignore comments (lines starting with :) and other fields
    
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
# 
# Events are buffered in memory and periodically flushed to Delta tables.
# This avoids writing on every single event (which would thrash Delta with tiny commits).

# CELL ********************

from pyspark.sql import functions as F
from pyspark.sql.types import *
from delta.tables import DeltaTable

class EventBuffer:
    """Thread-safe buffer that collects SSE events and flushes them to Delta."""
    
    def __init__(self, spark_session):
        self.spark = spark_session
        self._lock = threading.Lock()
        # Buffers keyed by table name: {"adds": [...], "updates": [...], "removes": [...], "reset": None|[...]}
        self._buffers = defaultdict(lambda: {"adds": [], "updates": [], "removes": [], "reset": None})
        self._stats = defaultdict(lambda: {"adds": 0, "updates": 0, "removes": 0, "resets": 0, "flushes": 0, "errors": 0})
    
    def add_event(self, table_name: str, event_type: str, data):
        """Buffer an incoming SSE event."""
        with self._lock:
            buf = self._buffers[table_name]
            if event_type == "reset":
                # Reset replaces everything — clear pending adds/updates/removes
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
        """Flush all buffered events to Delta tables."""
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
    
    def _flatten_entity(self, entity: dict) -> dict:
        """Flatten a JSON:API entity into a flat dict for Delta storage."""
        flat = {"id": entity.get("id"), "type": entity.get("type")}
        
        # Flatten attributes
        for key, value in entity.get("attributes", {}).items():
            if isinstance(value, (dict, list)):
                flat[f"attr_{key}"] = json.dumps(value)
            else:
                flat[f"attr_{key}"] = value
        
        # Flatten relationships to IDs
        for rel_name, rel_data in entity.get("relationships", {}).items():
            rel_inner = rel_data.get("data")
            if isinstance(rel_inner, dict):
                flat[f"rel_{rel_name}_id"] = rel_inner.get("id")
                flat[f"rel_{rel_name}_type"] = rel_inner.get("type")
            elif isinstance(rel_inner, list):
                flat[f"rel_{rel_name}_ids"] = json.dumps([r.get("id") for r in rel_inner])
            # else: null relationship, skip
        
        # Metadata columns
        flat["_ingested_at"] = datetime.now(timezone.utc).isoformat()
        
        return flat
    
    def _entities_to_df(self, entities: list):
        """Convert a list of JSON:API entities to a Spark DataFrame."""
        if not entities:
            return None
        flat_rows = [self._flatten_entity(e) for e in entities]
        return self.spark.createDataFrame(flat_rows)
    
    def _ensure_table_exists(self, table_name: str, df):
        """Create the Delta table if it doesn't exist yet."""
        full_table = f"bronze.mbta.{table_name}"
        if not self.spark.catalog.tableExists(full_table):
            print(f"[{datetime.now()}] Creating table {full_table}")
            df.write.format("delta").mode("overwrite").saveAsTable(full_table)
            return True
        return False
    
    def _flush_table(self, table_name: str, buf: dict):
        """Apply buffered events to a single Delta table."""
        full_table = f"bronze.mbta.{table_name}"
        has_changes = buf["reset"] is not None or buf["adds"] or buf["updates"] or buf["removes"]
        
        if not has_changes:
            return
        
        # Handle RESET — full table replacement
        if buf["reset"] is not None:
            df = self._entities_to_df(buf["reset"])
            if df is not None:
                df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(full_table)
                print(f"[{datetime.now()}] RESET {full_table}: {df.count()} entities")
            else:
                print(f"[{datetime.now()}] RESET {full_table}: empty dataset (cleared table)")
                self.spark.sql(f"DELETE FROM {full_table}")
            return
        
        # Handle incremental changes (add/update/remove) via MERGE
        upsert_entities = buf["adds"] + buf["updates"]
        remove_entities = buf["removes"]
        
        if upsert_entities:
            df = self._entities_to_df(upsert_entities)
            if df is not None:
                if self._ensure_table_exists(table_name, df):
                    # Table was just created from this data, no merge needed
                    count = len(upsert_entities)
                    print(f"[{datetime.now()}] INIT {full_table}: {count} entities")
                    return
                
                # Schema evolution: add new columns from incoming data
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
    
    def get_stats(self) -> dict:
        """Return ingestion statistics."""
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
# Each endpoint gets its own persistent SSE connection running in a background thread.
# Threads handle reconnection with exponential backoff on failures.

# CELL ********************

class SSEConsumerThread(threading.Thread):
    """Background thread that consumes SSE events from one MBTA endpoint."""
    
    def __init__(self, endpoint_name: str, config: dict, api_key: str, event_buffer: EventBuffer):
        super().__init__(daemon=True, name=f"sse-{endpoint_name}")
        self.endpoint_name = endpoint_name
        self.config = config
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
        url = f"{MBTA_API_BASE}{self.config['path']}"
        headers = {
            "Accept": "text/event-stream",
            "x-api-key": self.api_key,
        }
        table_name = self.config["table"]
        
        while not self._stop_event.is_set():
            client = None
            try:
                print(f"[{datetime.now()}] Connecting to SSE: {self.endpoint_name} ({url})")
                client = SSEClient(url, headers)
                client.connect()
                self._connected = True
                self._backoff = 1  # Reset backoff on successful connection
                print(f"[{datetime.now()}] Connected: {self.endpoint_name}")
                
                for event in client.events():
                    if self._stop_event.is_set():
                        break
                    self.event_buffer.add_event(table_name, event["event"], event["data"])
                    
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

# ## Orchestrator — Start All Streams & Periodic Flush
# 
# Launches all SSE consumer threads, then runs a flush loop that periodically
# writes buffered events to Delta tables. The notebook runs indefinitely until
# manually stopped or the Spark session times out.

# CELL ********************

from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()

# Initialize the shared event buffer
event_buffer = EventBuffer(spark)

# Launch one SSE consumer thread per endpoint
threads = {}
for name, config in ENDPOINTS.items():
    thread = SSEConsumerThread(name, config, api_key, event_buffer)
    thread.start()
    threads[name] = thread
    time.sleep(0.5)  # Stagger connections slightly to avoid rate-limit bursts

print(f"\nStarted {len(threads)} SSE consumer threads")
print("Flush interval: every {FLUSH_INTERVAL_SECONDS} seconds")
print("=" * 60)

# METADATA ********************

# META {
# META   "language": "python",
# META   "language_group": "synapse_pyspark"
# META }

# MARKDOWN ********************

# ## Main Loop — Flush & Monitor
# 
# This cell runs indefinitely. It:
# 1. Flushes buffered events to Delta every `FLUSH_INTERVAL_SECONDS`
# 2. Prints connection status and ingestion stats periodically
# 
# **To stop:** Interrupt the notebook or cancel the Spark session.

# CELL ********************

try:
    cycle = 0
    while True:
        time.sleep(FLUSH_INTERVAL_SECONDS)
        cycle += 1
        
        # Flush all buffered events to Delta
        event_buffer.flush_all()
        
        # Print status every 5 cycles (~2.5 min at 30s interval)
        if cycle % 5 == 0:
            stats = event_buffer.get_stats()
            connected = sum(1 for t in threads.values() if t.is_connected)
            print(f"\n[{datetime.now()}] STATUS: {connected}/{len(threads)} streams connected")
            for table, s in sorted(stats.items()):
                print(f"  {table:20s} | adds:{s['adds']:6d} | updates:{s['updates']:6d} | "
                      f"removes:{s['removes']:6d} | resets:{s['resets']:4d} | "
                      f"flushes:{s['flushes']:4d} | errors:{s['errors']:3d}")
            print()

except KeyboardInterrupt:
    print(f"\n[{datetime.now()}] Shutting down gracefully...")
finally:
    # Signal all threads to stop
    for name, thread in threads.items():
        thread.stop()
    
    # Final flush of any remaining buffered events
    print("Final flush...")
    event_buffer.flush_all()
    
    # Wait for threads to finish
    for name, thread in threads.items():
        thread.join(timeout=10)
    
    print(f"[{datetime.now()}] All SSE streams stopped.")
    
    # Print final stats
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
