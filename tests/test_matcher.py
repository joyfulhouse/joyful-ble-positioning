"""Tests for bounded BLE tracker validation and packet matching."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast
from uuid import UUID

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from custom_components.joyful_ble_positioning.const import (
    MAX_SCANNER_SOURCES,
    MAX_TRACKERS,
    WS_TYPE,
)
from custom_components.joyful_ble_positioning.matcher import match_tracker
from custom_components.joyful_ble_positioning.model import (
    SubscriptionValidationError,
    TrackerKind,
    TrackerSpec,
    parse_subscription_spec,
)

TRACKER_ID = "76a8b276-7e46-4a1a-a240-1b27c0c8f104"
OTHER_TRACKER_ID = "208406ac-1755-48d4-9157-8ee7e3e5054a"
IBEACON_UUID = "fda50693-a4e2-4fb1-afcf-c6eb07647825"


def _tracker(
    *,
    tracker_id: object = TRACKER_ID,
    kind: object = "static-mac",
    identity: object = "aa:bb:cc:dd:ee:ff",
) -> dict[str, object]:
    return {"trackerId": tracker_id, "kind": kind, "identity": identity}


def _request(
    *,
    trackers: object | None = None,
    scanner_sources: object | None = None,
    websocket_envelope: bool = False,
) -> dict[str, object]:
    request: dict[str, object] = {
        "trackers": [_tracker()] if trackers is None else trackers,
        "scannerSources": ["scanner-one"] if scanner_sources is None else scanner_sources,
    }
    if websocket_envelope:
        request.update({"id": 7, "type": WS_TYPE})
    return request


def _advertisement(
    *,
    manufacturer_data: Mapping[int, bytes] | None = None,
    rssi: int = -60,
    tx_power: int | None = -8,
) -> AdvertisementData:
    return AdvertisementData(
        local_name=None,
        manufacturer_data=dict(manufacturer_data or {}),
        service_data={},
        service_uuids=[],
        tx_power=tx_power,
        rssi=rssi,
        platform_data=(),
    )


def _cache_entry(
    address: str,
    *,
    manufacturer_data: Mapping[int, bytes] | None = None,
    rssi: int = -60,
    tx_power: int | None = -8,
) -> tuple[BLEDevice, AdvertisementData]:
    return (
        BLEDevice(address, None, {}),
        _advertisement(
            manufacturer_data=manufacturer_data,
            rssi=rssi,
            tx_power=tx_power,
        ),
    )


def _ibeacon_payload(
    *,
    uuid: str = IBEACON_UUID,
    major: int = 1,
    minor: int = 27,
    measured_power: int = -59,
) -> bytes:
    return (
        b"\x02\x15"
        + UUID(uuid).bytes
        + major.to_bytes(2, "big")
        + minor.to_bytes(2, "big")
        + measured_power.to_bytes(1, "big", signed=True)
    )


@pytest.mark.parametrize("kind", ["static-mac", "resolved-address"])
def test_parse_subscription_canonicalizes_mac_identity(kind: TrackerKind) -> None:
    spec = parse_subscription_spec(
        _request(trackers=[_tracker(kind=kind, identity="aa:bb:cc:dd:ee:ff")])
    )

    assert spec.trackers == (
        TrackerSpec(
            tracker_id=UUID(TRACKER_ID),
            kind=kind,
            identity="AA:BB:CC:DD:EE:FF",
        ),
    )
    assert spec.scanner_sources == ("scanner-one",)


def test_parse_subscription_canonicalizes_ibeacon_identity_and_tracker_uuid() -> None:
    spec = parse_subscription_spec(
        _request(
            trackers=[
                _tracker(
                    tracker_id=TRACKER_ID.upper(),
                    kind="ibeacon",
                    identity=f"{IBEACON_UUID.upper()}/1/27",
                )
            ]
        )
    )

    assert spec.trackers == (
        TrackerSpec(
            tracker_id=UUID(TRACKER_ID),
            kind="ibeacon",
            identity=f"{IBEACON_UUID}/1/27",
        ),
    )


def test_parse_subscription_accepts_the_websocket_request_envelope() -> None:
    spec = parse_subscription_spec(_request(websocket_envelope=True))

    assert spec.scanner_sources == ("scanner-one",)


@pytest.mark.parametrize(
    ("raw_message", "reason"),
    [
        (_request(trackers=[_tracker(), _tracker(tracker_id=TRACKER_ID.upper())]), "tracker"),
        (_request(scanner_sources=["scanner-one", "scanner-one"]), "source"),
    ],
)
def test_parse_subscription_rejects_duplicate_ids_and_sources(
    raw_message: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(SubscriptionValidationError, match=reason):
        parse_subscription_spec(raw_message)


@pytest.mark.parametrize(
    "identity",
    [
        "AA:BB:CC:DD:EE",
        "AA:BB:CC:DD:EE:FF:00",
        "AA:BB:CC:DD:EE:GG",
        "AA-BB-CC-DD-EE-FF",
    ],
)
def test_parse_subscription_rejects_malformed_or_non_six_byte_addresses(
    identity: str,
) -> None:
    with pytest.raises(SubscriptionValidationError, match="identity"):
        parse_subscription_spec(_request(trackers=[_tracker(identity=identity)]))


@pytest.mark.parametrize(
    "identity",
    [
        f"{IBEACON_UUID}/-1/27",
        f"{IBEACON_UUID}/65536/27",
        f"{IBEACON_UUID}/1/-1",
        f"{IBEACON_UUID}/1/65536",
        f"{IBEACON_UUID}/01/27",
        f"{IBEACON_UUID}/1/027",
    ],
)
def test_parse_subscription_rejects_out_of_range_or_noncanonical_ibeacon_numbers(
    identity: str,
) -> None:
    with pytest.raises(SubscriptionValidationError, match="identity"):
        parse_subscription_spec(_request(trackers=[_tracker(kind="ibeacon", identity=identity)]))


def test_parse_subscription_rejects_over_limit_lists() -> None:
    trackers = [_tracker(tracker_id=str(UUID(int=index + 1))) for index in range(MAX_TRACKERS + 1)]
    sources = [f"scanner-{index}" for index in range(MAX_SCANNER_SOURCES + 1)]

    with pytest.raises(SubscriptionValidationError, match="tracker count"):
        parse_subscription_spec(_request(trackers=trackers))
    with pytest.raises(SubscriptionValidationError, match="source count"):
        parse_subscription_spec(_request(scanner_sources=sources))


@pytest.mark.parametrize(
    "raw_message",
    [
        {**_request(), "unexpected": True},
        _request(trackers=[{**_tracker(), "unexpected": True}]),
        {"scannerSources": ["scanner-one"]},
        {"trackers": [_tracker()]},
        _request(trackers=[]),
        _request(scanner_sources=[]),
        _request(trackers="not-a-list"),
        _request(scanner_sources="not-a-list"),
    ],
)
def test_parse_subscription_rejects_unknown_missing_or_wrong_shape_fields(
    raw_message: dict[str, object],
) -> None:
    with pytest.raises(SubscriptionValidationError):
        parse_subscription_spec(raw_message)


@pytest.mark.parametrize(
    "tracker",
    [
        _tracker(tracker_id=7),
        _tracker(kind=7),
        _tracker(identity=7),
        _tracker(tracker_id="not-a-uuid"),
        _tracker(kind="unknown-kind"),
        _tracker(tracker_id="A" * 65),
        _tracker(kind="k" * 65),
        _tracker(identity="A" * 65),
    ],
)
def test_parse_subscription_rejects_non_string_invalid_or_oversized_tracker_fields(
    tracker: dict[str, object],
) -> None:
    with pytest.raises(SubscriptionValidationError):
        parse_subscription_spec(_request(trackers=[tracker]))


@pytest.mark.parametrize(
    "source",
    [7, "", " scanner-one", "scanner-one ", "scanner\x00one", "S" * 256],
)
def test_parse_subscription_rejects_invalid_or_oversized_sources(source: object) -> None:
    with pytest.raises(SubscriptionValidationError, match="source"):
        parse_subscription_spec(_request(scanner_sources=[source]))


def test_parse_subscription_validation_errors_do_not_echo_identity_values() -> None:
    private_identity = "private-tracker-identity-sentinel"
    private_source = " private-source-sentinel"

    for request, private_value in (
        (_request(trackers=[_tracker(identity=private_identity)]), private_identity),
        (_request(scanner_sources=[private_source]), private_source),
    ):
        with pytest.raises(SubscriptionValidationError) as raised:
            parse_subscription_spec(request)
        assert private_value not in str(raised.value)


@pytest.mark.parametrize("kind", ["static-mac", "resolved-address"])
def test_match_tracker_matches_exact_address_kinds(kind: TrackerKind) -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind=kind, identity="aa:bb:cc:dd:ee:ff")])
    ).trackers[0]
    cache = {"aa:bb:cc:dd:ee:ff": _cache_entry("aa:bb:cc:dd:ee:ff", rssi=-51)}

    result = match_tracker(tracker, cache, {"aa:bb:cc:dd:ee:ff": 123.5})

    assert result is not None
    assert result.tracker_id == UUID(TRACKER_ID)
    assert result.address == "aa:bb:cc:dd:ee:ff"
    assert result.rssi_dbm == -51
    assert result.tx_power_dbm == -8
    assert result.monotonic_timestamp == 123.5


def test_match_tracker_matches_exact_apple_ibeacon_payload() -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind="ibeacon", identity=f"{IBEACON_UUID.upper()}/1/27")])
    ).trackers[0]
    address = "AA:BB:CC:DD:EE:FF"
    cache = {
        address: _cache_entry(
            address,
            manufacturer_data={0x004C: _ibeacon_payload()},
            rssi=-48,
            tx_power=-12,
        )
    }

    result = match_tracker(tracker, cache, {address: 44.0})

    assert result is not None
    assert result.address == address
    assert result.rssi_dbm == -48
    assert result.tx_power_dbm == -12


@pytest.mark.parametrize(
    "manufacturer_data",
    [
        {0x004C: _ibeacon_payload()[:21]},
        {0x004C: _ibeacon_payload()[:22]},
        {0x004C: _ibeacon_payload() + b"\x00"},
        {0xFFFF: _ibeacon_payload()},
        {0x004C: b"\x02\x14" + _ibeacon_payload()[2:]},
        {0x004C: _ibeacon_payload(uuid="208406ac-1755-48d4-9157-8ee7e3e5054a")},
        {0x004C: _ibeacon_payload(major=2)},
        {0x004C: _ibeacon_payload(minor=28)},
    ],
)
def test_match_tracker_rejects_nonmatching_or_malformed_ibeacons(
    manufacturer_data: Mapping[int, bytes],
) -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind="ibeacon", identity=f"{IBEACON_UUID}/1/27")])
    ).trackers[0]
    address = "AA:BB:CC:DD:EE:FF"

    result = match_tracker(
        tracker,
        {address: _cache_entry(address, manufacturer_data=manufacturer_data)},
        {address: 10.0},
    )

    assert result is None


def test_match_tracker_chooses_newest_address_for_rotating_ibeacon() -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind="ibeacon", identity=f"{IBEACON_UUID}/1/27")])
    ).trackers[0]
    payload = {0x004C: _ibeacon_payload()}
    cache = {
        "AA:00:00:00:00:01": _cache_entry("AA:00:00:00:00:01", manufacturer_data=payload, rssi=-30),
        "AA:00:00:00:00:02": _cache_entry("AA:00:00:00:00:02", manufacturer_data=payload, rssi=-80),
    }

    result = match_tracker(
        tracker,
        cache,
        {"AA:00:00:00:00:01": 10.0, "AA:00:00:00:00:02": 11.0},
    )

    assert result is not None
    assert result.address == "AA:00:00:00:00:02"


def test_match_tracker_uses_stronger_rssi_when_timestamps_tie() -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind="ibeacon", identity=f"{IBEACON_UUID}/1/27")])
    ).trackers[0]
    payload = {0x004C: _ibeacon_payload()}
    cache = {
        "AA:00:00:00:00:01": _cache_entry("AA:00:00:00:00:01", manufacturer_data=payload, rssi=-70),
        "AA:00:00:00:00:02": _cache_entry("AA:00:00:00:00:02", manufacturer_data=payload, rssi=-40),
    }

    result = match_tracker(
        tracker,
        cache,
        {"AA:00:00:00:00:01": 10.0, "AA:00:00:00:00:02": 10.0},
    )

    assert result is not None
    assert result.address == "AA:00:00:00:00:02"


def test_match_tracker_uses_address_as_final_internal_tie_break() -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind="ibeacon", identity=f"{IBEACON_UUID}/1/27")])
    ).trackers[0]
    payload = {0x004C: _ibeacon_payload()}
    first = "AA:00:00:00:00:01"
    second = "AA:00:00:00:00:02"
    entries = {
        first: _cache_entry(first, manufacturer_data=payload, rssi=-50),
        second: _cache_entry(second, manufacturer_data=payload, rssi=-50),
    }

    forward = match_tracker(tracker, entries, {first: 10.0, second: 10.0})
    reverse = match_tracker(
        tracker,
        dict(reversed(entries.items())),
        {first: 10.0, second: 10.0},
    )

    assert forward is not None
    assert reverse is not None
    assert forward.address == first
    assert reverse.address == first


def test_match_tracker_ignores_cache_entries_without_a_timestamp() -> None:
    tracker = parse_subscription_spec(_request()).trackers[0]
    address = "AA:BB:CC:DD:EE:FF"

    assert match_tracker(tracker, {address: _cache_entry(address)}, {}) is None


def test_match_tracker_ignores_malformed_timestamps_independent_of_cache_order() -> None:
    tracker = parse_subscription_spec(
        _request(trackers=[_tracker(kind="ibeacon", identity=f"{IBEACON_UUID}/1/27")])
    ).trackers[0]
    payload = {0x004C: _ibeacon_payload()}
    valid = "AA:00:00:00:00:05"
    addresses = [
        valid,
        "AA:00:00:00:00:01",
        "AA:00:00:00:00:02",
        "AA:00:00:00:00:03",
        "AA:00:00:00:00:04",
    ]
    entries = {
        address: _cache_entry(address, manufacturer_data=payload, rssi=-20) for address in addresses
    }
    timestamps = cast(
        Mapping[str, float],
        {
            valid: 0,
            "AA:00:00:00:00:01": True,
            "AA:00:00:00:00:02": "999",
            "AA:00:00:00:00:03": float("nan"),
            "AA:00:00:00:00:04": float("inf"),
        },
    )

    forward = match_tracker(tracker, entries, timestamps)
    reverse = match_tracker(
        tracker,
        dict(reversed(entries.items())),
        dict(reversed(tuple(timestamps.items()))),
    )

    assert forward is not None
    assert reverse is not None
    assert forward.address == valid
    assert reverse.address == valid
    assert forward.monotonic_timestamp == 0.0
    assert type(forward.monotonic_timestamp) is float
