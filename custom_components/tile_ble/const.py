"""Constants for tile_ble."""
DOMAIN = "tile_ble"
TILE_SERVICE_UUID = "0000feed-0000-1000-8000-00805f9b34fb"

CONF_TILES = "tiles"
CONF_TIMEOUT = "timeout"
DEFAULT_TIMEOUT = 15  # minutes without an advertisement before the tracker is considered away

CONF_BATTERY_ENABLED = "battery_enabled"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
BATTERY_INTERVAL_HOURS = 24  # the only place where this integration calls the cloud

SIGNAL_SEEN = f"{DOMAIN}_seen_{{entry_id}}"
SIGNAL_BATTERY = f"{DOMAIN}_battery_{{entry_id}}"
