"""
WMATA Real-Time Ingestion — Azure Function

Timer-triggered function that polls WMATA real-time endpoints every 20 seconds
and publishes events to Azure Event Hub for downstream consumption by Fabric Eventstream.

Streams:
  - rail_predictions: Train arrival predictions for all stations
  - bus_positions: Real-time GPS positions for all buses
  - rail_incidents: Rail service disruptions/alerts
  - bus_incidents: Bus service disruptions/alerts
"""

import azure.functions as func
import logging
import json
import os
import requests
from datetime import datetime, timezone
from azure.eventhub import EventHubProducerClient, EventData

app = func.FunctionApp()

WMATA_API_KEY = os.environ["WMATA_API_KEY"]
EVENT_HUB_CONNECTION = os.environ["EVENT_HUB_CONNECTION"]
EVENT_HUB_NAME = os.environ.get("EVENT_HUB_NAME", "wmata-realtime")
WMATA_BASE_URL = "https://api.wmata.com"

HEADERS = {"api_key": WMATA_API_KEY, "Accept": "application/json"}

# Endpoint configuration: name → (path, response_key, partition_key)
REALTIME_FEEDS = {
    "rail_predictions": {
        "path": "/StationPrediction.svc/json/GetPrediction/All",
        "response_key": "Trains",
        "id_field": None,  # Predictions don't have stable IDs
    },
    "bus_positions": {
        "path": "/Bus.svc/json/jBusPositions",
        "response_key": "BusPositions",
        "id_field": "VehicleID",
    },
    "rail_incidents": {
        "path": "/Incidents.svc/json/Incidents",
        "response_key": "Incidents",
        "id_field": "IncidentID",
    },
    "bus_incidents": {
        "path": "/Incidents.svc/json/BusIncidents",
        "response_key": "BusIncidents",
        "id_field": "IncidentID",
    },
}


def fetch_wmata_feed(feed_name: str, feed_config: dict) -> list[dict]:
    """Fetch a single WMATA real-time feed."""
    url = f"{WMATA_BASE_URL}{feed_config['path']}"
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        data = response.json()
        records = data.get(feed_config["response_key"], [])
        return records
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch {feed_name}: {e}")
        return []


def publish_to_event_hub(events: list[dict]):
    """Publish a batch of events to Event Hub."""
    if not events:
        return 0

    producer = EventHubProducerClient.from_connection_string(
        conn_str=EVENT_HUB_CONNECTION, eventhub_name=EVENT_HUB_NAME
    )
    try:
        batch = producer.create_batch()
        count = 0
        for event in events:
            event_data = EventData(json.dumps(event))
            event_data.properties = {"feed": event.get("_feed_name", "unknown")}
            try:
                batch.add(event_data)
                count += 1
            except ValueError:
                # Batch is full, send it and start a new one
                producer.send_batch(batch)
                batch = producer.create_batch()
                batch.add(event_data)
                count += 1
        if count > 0:
            producer.send_batch(batch)
        return count
    finally:
        producer.close()


# High-frequency feeds: rail predictions + bus positions (every 20 seconds)
@app.timer_trigger(
    schedule="*/20 * * * * *",
    arg_name="timer",
    run_on_startup=False,
)
def wmata_realtime_high_freq(timer: func.TimerRequest) -> None:
    """Poll high-frequency WMATA feeds (predictions, positions) every 20 seconds."""
    if timer.past_due:
        logging.warning("Timer is past due — skipping this execution")
        return

    ingestion_ts = datetime.now(timezone.utc).isoformat()
    all_events = []

    for feed_name in ["rail_predictions", "bus_positions"]:
        feed_config = REALTIME_FEEDS[feed_name]
        records = fetch_wmata_feed(feed_name, feed_config)

        for record in records:
            record["_feed_name"] = feed_name
            record["_ingested_at"] = ingestion_ts
            if feed_config["id_field"] and feed_config["id_field"] in record:
                record["_entity_id"] = str(record[feed_config["id_field"]])

        all_events.extend(records)

    published = publish_to_event_hub(all_events)
    logging.info(
        f"[high-freq] Published {published} events "
        f"(predictions={len([e for e in all_events if e.get('_feed_name') == 'rail_predictions'])} "
        f"positions={len([e for e in all_events if e.get('_feed_name') == 'bus_positions'])})"
    )


# Low-frequency feeds: incidents (every 60 seconds)
@app.timer_trigger(
    schedule="0 * * * * *",
    arg_name="timer",
    run_on_startup=False,
)
def wmata_realtime_incidents(timer: func.TimerRequest) -> None:
    """Poll WMATA incident feeds every 60 seconds."""
    if timer.past_due:
        logging.warning("Timer is past due — skipping this execution")
        return

    ingestion_ts = datetime.now(timezone.utc).isoformat()
    all_events = []

    for feed_name in ["rail_incidents", "bus_incidents"]:
        feed_config = REALTIME_FEEDS[feed_name]
        records = fetch_wmata_feed(feed_name, feed_config)

        for record in records:
            record["_feed_name"] = feed_name
            record["_ingested_at"] = ingestion_ts
            if feed_config["id_field"] and feed_config["id_field"] in record:
                record["_entity_id"] = str(record[feed_config["id_field"]])

        all_events.extend(records)

    published = publish_to_event_hub(all_events)
    logging.info(
        f"[incidents] Published {published} events "
        f"(rail={len([e for e in all_events if e.get('_feed_name') == 'rail_incidents'])} "
        f"bus={len([e for e in all_events if e.get('_feed_name') == 'bus_incidents'])})"
    )
