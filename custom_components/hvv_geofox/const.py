"""Constants for HVV Geofox integration."""

DOMAIN = "hvv_geofox"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_STATIONS = "stations"
CONF_POLL_INTERVAL = "poll_interval"
CONF_WALK_OFFSET = "walk_offset"

DEFAULT_POLL_INTERVAL = 30   # seconds – safe default
DEFAULT_WALK_OFFSET = 0      # minutes
MIN_POLL_INTERVAL = 2
MAX_POLL_INTERVAL = 30
