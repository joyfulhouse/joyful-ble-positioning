"""Pure matching against Home Assistant's public BLE scanner cache mappings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from .model import TrackerSpec

_APPLE_COMPANY_ID = 0x004C
_IBEACON_PREFIX = b"\x02\x15"
_IBEACON_PAYLOAD_LENGTH = 23


@dataclass(frozen=True, slots=True)
class MatchResult:
    """An internal match; address must never cross the public event boundary."""

    tracker_id: UUID
    address: str
    rssi_dbm: int
    tx_power_dbm: int | None
    monotonic_timestamp: float


def _matches_ibeacon(tracker: TrackerSpec, advertisement: AdvertisementData) -> bool:
    payload = advertisement.manufacturer_data.get(_APPLE_COMPANY_ID)
    if (
        payload is None
        or len(payload) != _IBEACON_PAYLOAD_LENGTH
        or payload[:2] != _IBEACON_PREFIX
    ):
        return False

    uuid_text, major_text, minor_text = tracker.identity.split("/")
    return (
        payload[2:18] == UUID(uuid_text).bytes
        and int.from_bytes(payload[18:20], "big") == int(major_text)
        and int.from_bytes(payload[20:22], "big") == int(minor_text)
    )


def _matches(
    tracker: TrackerSpec,
    address: str,
    advertisement: AdvertisementData,
) -> bool:
    if tracker.kind == "ibeacon":
        return _matches_ibeacon(tracker, advertisement)
    return address.upper() == tracker.identity


def match_tracker(
    tracker: TrackerSpec,
    advertisements: Mapping[str, tuple[BLEDevice, AdvertisementData]],
    timestamps: Mapping[str, float],
) -> MatchResult | None:
    """Return the deterministic freshest cache match for one tracker."""
    matches: list[MatchResult] = []
    for address, (_device, advertisement) in advertisements.items():
        timestamp = timestamps.get(address)
        if timestamp is None or not _matches(tracker, address, advertisement):
            continue
        matches.append(
            MatchResult(
                tracker_id=tracker.tracker_id,
                address=address,
                rssi_dbm=advertisement.rssi,
                tx_power_dbm=advertisement.tx_power,
                monotonic_timestamp=timestamp,
            )
        )

    if not matches:
        return None
    return min(
        matches,
        key=lambda result: (
            -result.monotonic_timestamp,
            -result.rssi_dbm,
            result.address.upper(),
            result.address,
        ),
    )
