# =============================================================================
# transit-analytics — Variable Library
# =============================================================================
# Centralized configuration for external resources and service endpoints.
# Import this in notebooks with: %run ./config
# Or in Python: exec(open("/path/to/config.py").read())
#
# What belongs here:
#   ✓ External service URLs and endpoints
#   ✓ Azure resource names and identifiers
#   ✓ Secret names (not values!)
#   ✓ API configuration (rate limits, timeouts)
#
# What does NOT belong here:
#   ✗ Table names (owned by the notebooks that create them)
#   ✗ Schema logic or transformations
#   ✗ Secrets or credentials
# =============================================================================

# ---------------------------------------------------------------------------
# Azure Key Vault
# ---------------------------------------------------------------------------
KEYVAULT_URL = "https://kvtransitdemo-f70cfb6a.vault.azure.net/"
SECRET_MBTA_API_KEY = "mbta-api-key"
SECRET_WMATA_API_KEY = "wmata-api-key"
SECRET_WEATHER_API_KEY = "weather-api-key"

# ---------------------------------------------------------------------------
# MBTA V3 API
# ---------------------------------------------------------------------------
MBTA_API_BASE = "https://api-v3.mbta.com"

# SSE-capable endpoints (all support Accept: text/event-stream)
MBTA_SSE_ENDPOINTS = {
    "routes":          {"path": "/routes"},
    "stops":           {"path": "/stops"},
    "lines":           {"path": "/lines"},
    "shapes":          {"path": "/shapes"},
    "route_patterns":  {"path": "/route_patterns"},
    "facilities":      {"path": "/facilities"},
    "services":        {"path": "/services"},
    "schedules":       {"path": "/schedules"},
    "predictions":     {"path": "/predictions"},
    "vehicles":        {"path": "/vehicles"},
    "alerts":          {"path": "/alerts"},
    "trips":           {"path": "/trips"},
    "live_facilities": {"path": "/live_facilities"},
}

# Batch-only endpoints (require filters, no unfiltered SSE)
MBTA_BATCH_ENDPOINTS = {
    "stop_events": {"path": "/stop_events"},
}

# GTFS Realtime Enhanced JSON feeds (alternative to SSE — full snapshot per request)
MBTA_GTFS_RT_FEEDS = {
    "vehicle_positions": "https://cdn.mbta.com/realtime/VehiclePositions_enhanced.json",
    "trip_updates":      "https://cdn.mbta.com/realtime/TripUpdates_enhanced.json",
    "alerts":            "https://cdn.mbta.com/realtime/Alerts_enhanced.json",
}

# GTFS Static feed
MBTA_GTFS_STATIC_URL = "https://cdn.mbta.com/MBTA_GTFS.zip"

# ---------------------------------------------------------------------------
# MBTA API Configuration
# ---------------------------------------------------------------------------
MBTA_API_RATE_LIMIT_PER_MINUTE = 1000  # With API key
MBTA_API_TIMEOUT_SECONDS = 30
MBTA_SSE_KEEPALIVE_TIMEOUT_SECONDS = 90
MBTA_SSE_MAX_BACKOFF_SECONDS = 300
MBTA_SSE_FLUSH_INTERVAL_SECONDS = 30

# ---------------------------------------------------------------------------
# WMATA (future)
# ---------------------------------------------------------------------------
WMATA_API_BASE = "https://api.wmata.com"

# ---------------------------------------------------------------------------
# Weather (future)
# ---------------------------------------------------------------------------
# WEATHER_API_BASE = ""  # TBD — OpenWeatherMap, NOAA, etc.

# ---------------------------------------------------------------------------
# Fabric Workspace
# ---------------------------------------------------------------------------
FABRIC_WORKSPACE_ID = "c030c477-6e50-4334-8fcb-fd032f8870b9"
LAKEHOUSE_BRONZE_ID = "e7c516ba-df49-4dc6-9f32-299392d999c9"
