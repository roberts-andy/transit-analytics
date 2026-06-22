# WMATA Real-Time Ingestion — Azure Function

Timer-triggered Azure Function that polls WMATA real-time REST APIs every 20 seconds and publishes events to Azure Event Hub for consumption by Fabric Eventstream.

## Endpoints Polled

| Stream | WMATA Endpoint | Refresh Rate |
|--------|---------------|-------------|
| Rail Predictions | `/StationPrediction.svc/json/GetPrediction/All` | ~20s |
| Bus Positions | `/Bus.svc/json/jBusPositions` | ~20s |
| Rail Incidents | `/Incidents.svc/json/Incidents` | ~60s |
| Bus Incidents | `/Incidents.svc/json/BusIncidents` | ~60s |

## Architecture

```
Azure Function (Timer: 20s)
    → polls WMATA REST APIs
    → publishes JSON events to Event Hub (partitioned by feed type)
    → Fabric Eventstream consumes from Event Hub
    → Lands in bronze.wmata.* Delta tables
```

## Configuration

All secrets are stored in Azure Key Vault:
- `wmata-api-key` — WMATA developer API key

App Settings (from Key Vault references):
- `WMATA_API_KEY` — Key Vault reference
- `EVENT_HUB_CONNECTION` — Event Hub connection string
- `EVENT_HUB_NAME` — Event Hub name

## Local Development

```bash
cd functions/wmata-realtime-ingest
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
func start
```
