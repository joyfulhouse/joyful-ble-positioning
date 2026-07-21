"""Constants for Joyful BLE Positioning."""

from typing import Final

DOMAIN: Final = "joyful_ble_positioning"
INTEGRATION_VERSION: Final = "0.1.0"

SAMPLE_INTERVAL_SECONDS: Final = 0.25
STALE_AFTER_SECONDS: Final = 10.0
MAX_TRACKERS: Final = 8
MAX_SCANNER_SOURCES: Final = 64
MAX_SUBSCRIPTIONS: Final = 4

WS_TYPE: Final = "joyful_ble_positioning/subscribe_observations"
