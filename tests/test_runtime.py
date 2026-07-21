"""Tests for the shared public-cache BLE observation sampler."""

from __future__ import annotations

import asyncio
import gc
import importlib
import inspect
import math
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BaseHaScanner
from homeassistant.core import HomeAssistant

from custom_components.joyful_ble_positioning.const import MAX_SUBSCRIPTIONS
from custom_components.joyful_ble_positioning.model import SubscriptionSpec, TrackerSpec

RUNTIME_MODULE = "custom_components.joyful_ble_positioning.runtime"
TRACKER_ONE_ID = UUID("76a8b276-7e46-4a1a-a240-1b27c0c8f104")
TRACKER_TWO_ID = UUID("208406ac-1755-48d4-9157-8ee7e3e5054a")
IBEACON_UUID = UUID("fda50693-a4e2-4fb1-afcf-c6eb07647825")
ADDRESS_ONE = "AA:BB:CC:DD:EE:01"
ADDRESS_TWO = "AA:BB:CC:DD:EE:02"


def _runtime_module() -> Any:
    module_spec = importlib.util.find_spec(RUNTIME_MODULE)
    assert module_spec is not None, "runtime module must exist"
    return importlib.import_module(RUNTIME_MODULE)


def _tracker(
    tracker_id: UUID = TRACKER_ONE_ID,
    address: str = ADDRESS_ONE,
) -> TrackerSpec:
    return TrackerSpec(tracker_id=tracker_id, kind="static-mac", identity=address)


def _spec(
    *,
    trackers: tuple[TrackerSpec, ...] | None = None,
    sources: tuple[str, ...] = ("scanner-one",),
) -> SubscriptionSpec:
    return SubscriptionSpec(trackers=trackers or (_tracker(),), scanner_sources=sources)


def _ignore_event(event: dict[str, object]) -> None:
    del event


def _advertisement(
    *,
    rssi: object = -60,
    tx_power: object = -8,
    private_name: str | None = None,
) -> AdvertisementData:
    return AdvertisementData(
        local_name=private_name,
        manufacturer_data={0xFFFF: b"private-manufacturer-bytes"},
        service_data={"private-service": b"private-service-bytes"},
        service_uuids=["private-service-uuid"],
        tx_power=cast(int | None, tx_power),
        rssi=cast(int, rssi),
        platform_data=("private-platform-data",),
    )


def _cache_entry(
    address: str,
    *,
    rssi: object = -60,
    tx_power: object = -8,
    private_name: str | None = None,
) -> tuple[BLEDevice, AdvertisementData]:
    return (
        BLEDevice(address, private_name, {"private-device-detail": "private"}),
        _advertisement(rssi=rssi, tx_power=tx_power, private_name=private_name),
    )


def _ibeacon_cache_entry(address: str, *, rssi: object) -> tuple[BLEDevice, AdvertisementData]:
    payload = b"\x02\x15" + IBEACON_UUID.bytes + b"\x00\x01\x00\x1b\xc5"
    return (
        BLEDevice(address, None, {}),
        AdvertisementData(
            local_name=None,
            manufacturer_data={0x004C: payload},
            service_data={},
            service_uuids=[],
            tx_power=-8,
            rssi=cast(int, rssi),
            platform_data=(),
        ),
    )


class FakeScanner(BaseHaScanner):
    """A BaseHaScanner-shaped source with observable public property reads."""

    __slots__ = (
        "advertisement_error",
        "advertisement_reads",
        "advertisements",
        "timestamp_error",
        "timestamp_reads",
        "timestamps",
    )

    def __init__(
        self,
        source: str,
        *,
        connectable: bool,
        advertisements: object | None = None,
        timestamps: object | None = None,
        advertisement_error: BaseException | None = None,
        timestamp_error: BaseException | None = None,
    ) -> None:
        # Deliberately avoid BaseHaScanner.__init__: it requires a configured
        # global Bluetooth manager, while these tests exercise only public data.
        self.source = source
        self.connectable = connectable
        self.advertisements = {} if advertisements is None else advertisements
        self.timestamps = {} if timestamps is None else timestamps
        self.advertisement_error = advertisement_error
        self.timestamp_error = timestamp_error
        self.advertisement_reads = 0
        self.timestamp_reads = 0

    @property
    def discovered_devices_and_advertisement_data(
        self,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        self.advertisement_reads += 1
        if self.advertisement_error is not None:
            raise self.advertisement_error
        value = self.advertisements
        if callable(value):
            value = value()
        return cast(dict[str, tuple[BLEDevice, AdvertisementData]], value)

    @property
    def discovered_device_timestamps(self) -> dict[str, float]:
        self.timestamp_reads += 1
        if self.timestamp_error is not None:
            raise self.timestamp_error
        value = self.timestamps
        if callable(value):
            value = value()
        return cast(dict[str, float], value)


class FaultyMapping(Mapping[str, object]):
    """A mapping whose key iteration fails without exposing its message."""

    def __getitem__(self, key: str) -> object:
        raise KeyError("private-key-fault-sentinel")

    def __iter__(self) -> Any:
        raise KeyError("private-key-fault-sentinel")

    def __len__(self) -> int:
        return 1


class WeakMapping(dict[str, object]):
    """Weak-referenceable cache mapping used to prove tick-local retention."""


class ExplodingScannerList(list[BaseHaScanner]):
    """A nominal list whose iteration fails with sensitive detail."""

    def __iter__(self) -> Any:
        raise RuntimeError("private-enumeration-sentinel")


@dataclass(slots=True)
class Clock:
    monotonic: float = 100.0
    wall: datetime = datetime(2026, 7, 21, 12, 0, 0, 123456, tzinfo=UTC)
    monotonic_calls: int = 0
    wall_calls: int = 0

    def monotonic_now(self) -> float:
        self.monotonic_calls += 1
        return self.monotonic

    def wall_now(self) -> datetime:
        self.wall_calls += 1
        return self.wall


@dataclass(slots=True)
class ControlledSleep:
    calls: list[float] = field(default_factory=list)
    waiters: list[Any] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        await waiter

    async def release(self) -> None:
        for _ in range(100):
            if self.waiters:
                waiter = self.waiters.pop(0)
                if waiter.done():
                    continue
                waiter.set_result(None)
                for _ in range(3):
                    await asyncio.sleep(0)
                return
            await asyncio.sleep(0)
        raise AssertionError("sampler did not enter its scheduled sleep")


@dataclass(slots=True)
class RuntimeHarness:
    module: Any
    hass: HomeAssistant
    clock: Clock
    sleeper: ControlledSleep
    scanners: list[FakeScanner] = field(default_factory=list)
    lookup_overrides: dict[str, BaseHaScanner | None | BaseException] = field(default_factory=dict)
    current_calls: int = 0
    lookup_calls: list[str] = field(default_factory=list)
    runtimes: list[Any] = field(default_factory=list)

    def current_scanners(self, hass: HomeAssistant) -> list[BaseHaScanner]:
        assert hass is self.hass
        self.current_calls += 1
        return list(self.scanners)

    def scanner_by_source(self, hass: HomeAssistant, source: str) -> BaseHaScanner | None:
        assert hass is self.hass
        self.lookup_calls.append(source)
        override = self.lookup_overrides.get(source)
        if isinstance(override, BaseException):
            raise override
        if source in self.lookup_overrides:
            return override
        return next((scanner for scanner in self.scanners if scanner.source == source), None)

    def make_runtime(self) -> Any:
        sampler = self.module.BleObservationRuntime(self.hass, sleep=self.sleeper)
        self.runtimes.append(sampler)
        return sampler


@pytest.fixture
async def harness(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> RuntimeHarness:
    module = _runtime_module()
    runtime_harness = RuntimeHarness(
        module=module,
        hass=hass,
        clock=Clock(),
        sleeper=ControlledSleep(),
    )
    monkeypatch.setattr(
        module.ha_bluetooth,
        "async_current_scanners",
        runtime_harness.current_scanners,
    )
    monkeypatch.setattr(
        module.ha_bluetooth,
        "async_scanner_by_source",
        runtime_harness.scanner_by_source,
    )
    monkeypatch.setattr(
        module.bluetooth_data_tools,
        "monotonic_time_coarse",
        runtime_harness.clock.monotonic_now,
    )
    monkeypatch.setattr(module, "_utc_now", runtime_harness.clock.wall_now)
    yield runtime_harness
    for sampler in runtime_harness.runtimes:
        await sampler.async_close()


def test_runtime_module_exists() -> None:
    assert importlib.util.find_spec(RUNTIME_MODULE) is not None


def test_pinned_public_api_has_exact_supported_signatures() -> None:
    from homeassistant.components import bluetooth as ha_bluetooth

    assert tuple(inspect.signature(ha_bluetooth.async_current_scanners).parameters) == ("hass",)
    assert tuple(inspect.signature(ha_bluetooth.async_scanner_by_source).parameters) == (
        "hass",
        "source",
    )


def test_capability_check_accepts_zero_scanners(harness: RuntimeHarness) -> None:
    harness.module.capability_check(harness.hass)

    assert harness.current_calls == 1


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("async_current_scanners", None),
        ("async_current_scanners", lambda: []),
        ("async_current_scanners", lambda hass, connectable: []),
        ("async_scanner_by_source", None),
        ("async_scanner_by_source", lambda source: None),
        ("async_scanner_by_source", lambda hass, source, connectable: None),
    ],
)
def test_capability_check_rejects_missing_or_wrong_arity_public_api(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    replacement: Callable[..., object] | None,
) -> None:
    if replacement is None:
        monkeypatch.delattr(harness.module.ha_bluetooth, name)
    else:
        monkeypatch.setattr(harness.module.ha_bluetooth, name, replacement)

    with pytest.raises(harness.module.IncompatibleBluetoothApiError):
        harness.module.capability_check(harness.hass)


def test_current_scanners_rejects_duplicate_sources(harness: RuntimeHarness) -> None:
    harness.scanners.extend(
        [
            FakeScanner("duplicate", connectable=True),
            FakeScanner("duplicate", connectable=False),
        ]
    )

    with pytest.raises(harness.module.IncompatibleBluetoothApiError, match="duplicate"):
        harness.module.current_scanners(harness.hass)


@pytest.mark.parametrize("returned", [{}, (), "scanner", object()])
def test_current_scanners_rejects_wrong_enumeration_return_type(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
    returned: object,
) -> None:
    def wrong_current_scanners(hass: HomeAssistant) -> object:
        assert hass is harness.hass
        return returned

    monkeypatch.setattr(
        harness.module.ha_bluetooth,
        "async_current_scanners",
        wrong_current_scanners,
    )

    with pytest.raises(harness.module.IncompatibleBluetoothApiError):
        harness.module.current_scanners(harness.hass)


def test_current_scanners_wraps_enumeration_iteration_fault_privacy_safely(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_current_scanners(hass: HomeAssistant) -> list[BaseHaScanner]:
        assert hass is harness.hass
        return ExplodingScannerList()

    monkeypatch.setattr(
        harness.module.ha_bluetooth,
        "async_current_scanners",
        exploding_current_scanners,
    )

    with pytest.raises(harness.module.IncompatibleBluetoothApiError) as raised:
        harness.module.current_scanners(harness.hass)

    assert "private-enumeration-sentinel" not in str(raised.value)


@pytest.mark.parametrize("source", [None, 7, ""])
def test_current_scanners_rejects_missing_non_string_or_empty_source(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
    source: object,
) -> None:
    if source == 7:
        monkeypatch.setattr(harness.module, "BaseHaScanner", object)
        harness.scanners.append(cast(FakeScanner, SimpleNamespace(source=source)))
    else:
        scanner = FakeScanner("temporary", connectable=True)
        if source is None:
            del scanner.source
        else:
            scanner.source = cast(str, source)
        harness.scanners.append(scanner)

    with pytest.raises(harness.module.IncompatibleBluetoothApiError):
        harness.module.current_scanners(harness.hass)


@pytest.mark.asyncio
async def test_subscription_rejects_unknown_or_mismatched_lookup_without_cache_read(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()

    with pytest.raises(harness.module.ScannerSourceUnavailableError):
        await sampler.async_subscribe(_spec(sources=("unknown",)), _ignore_event)

    harness.lookup_overrides["scanner-one"] = FakeScanner("different", connectable=False)
    with pytest.raises(harness.module.ScannerSourceUnavailableError):
        await sampler.async_subscribe(_spec(), _ignore_event)

    assert scanner.advertisement_reads == 0
    assert scanner.timestamp_reads == 0


@pytest.mark.asyncio
async def test_subscription_reports_lookup_fault_as_api_incompatibility(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    harness.lookup_overrides["scanner-one"] = RuntimeError("private-lookup-sentinel")
    sampler = harness.make_runtime()

    with pytest.raises(harness.module.IncompatibleBluetoothApiError) as raised:
        await sampler.async_subscribe(_spec(), _ignore_event)

    assert "private-lookup-sentinel" not in str(raised.value)
    assert sampler.subscription_count == 0


@pytest.mark.asyncio
async def test_subscription_accepts_equivalent_current_scanner_source_without_cache_read(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    harness.lookup_overrides["scanner-one"] = FakeScanner("scanner-one", connectable=False)
    sampler = harness.make_runtime()

    subscription = await sampler.async_subscribe(_spec(), _ignore_event)

    assert sampler.subscription_count == 1
    assert scanner.advertisement_reads == scanner.timestamp_reads == 0
    subscription.cancel()


def test_exact_wire_serialization_minimizes_observed_and_stale_events() -> None:
    module = _runtime_module()
    observed = module.ObservedEvent(
        stream_id="stream-one",
        state="observed",
        tracker_id=str(TRACKER_ONE_ID),
        scanner_source="scanner-one",
        sequence=1,
        rssi_dbm=-61,
        tx_power_dbm=None,
        age_ms=125,
        received_at="2026-07-21T12:00:00.000Z",
    )
    stale = module.StaleEvent(
        stream_id="stream-one",
        state="stale",
        tracker_id=str(TRACKER_ONE_ID),
        scanner_source="scanner-one",
        sequence=2,
        age_ms=10_001,
    )

    assert module.as_wire_event(observed) == {
        "streamId": "stream-one",
        "state": "observed",
        "trackerId": str(TRACKER_ONE_ID),
        "scannerSource": "scanner-one",
        "sequence": 1,
        "rssiDbm": -61,
        "txPowerDbm": None,
        "ageMs": 125,
        "receivedAt": "2026-07-21T12:00:00.000Z",
    }
    assert module.as_wire_event(stale) == {
        "streamId": "stream-one",
        "state": "stale",
        "trackerId": str(TRACKER_ONE_ID),
        "scannerSource": "scanner-one",
        "sequence": 2,
        "ageMs": 10_001,
    }


@pytest.mark.parametrize(
    "timestamp",
    [True, "100", math.nan, math.inf, -math.inf, 101.0],
)
@pytest.mark.asyncio
async def test_invalid_or_implausibly_future_timestamp_emits_initial_stale(
    harness: RuntimeHarness,
    timestamp: object,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: timestamp},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [event["state"] for event in events] == ["stale"]
    assert events[0]["ageMs"] == 10_000


@pytest.mark.parametrize("rssi", [True, -127, -128, 0, 1, -60.0, math.nan])
@pytest.mark.asyncio
async def test_invalid_rssi_emits_initial_stale(
    harness: RuntimeHarness,
    rssi: object,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE, rssi=rssi)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert events[0]["state"] == "stale"


@pytest.mark.asyncio
async def test_invalid_newest_ibeacon_rssi_does_not_hide_older_valid_match(
    harness: RuntimeHarness,
) -> None:
    invalid_address = "AA:BB:CC:DD:EE:10"
    valid_address = "AA:BB:CC:DD:EE:11"
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={
            invalid_address: _ibeacon_cache_entry(invalid_address, rssi=True),
            valid_address: _ibeacon_cache_entry(valid_address, rssi=-70),
        },
        timestamps={invalid_address: 100.0, valid_address: 99.9},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    tracker = TrackerSpec(
        tracker_id=TRACKER_ONE_ID,
        kind="ibeacon",
        identity=f"{IBEACON_UUID}/1/27",
    )
    subscription = await sampler.async_subscribe(_spec(trackers=(tracker,)), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert events[0]["state"] == "observed"
    assert events[0]["rssiDbm"] == -70


@pytest.mark.parametrize("tx_power", [None, True, -128, -127, 21, -8.0, math.nan, math.inf])
@pytest.mark.asyncio
async def test_invalid_tx_power_retains_observation_with_null_tx(
    harness: RuntimeHarness,
    tx_power: object,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=False,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE, tx_power=tx_power)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert events[0]["state"] == "observed"
    assert events[0]["txPowerDbm"] is None


@pytest.mark.parametrize(("rssi", "tx_power"), [(-126, -126), (-60, 0), (-1, 20)])
@pytest.mark.asyncio
async def test_inclusive_signal_boundaries_are_observed(
    harness: RuntimeHarness,
    rssi: int,
    tx_power: int,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE, rssi=rssi, tx_power=tx_power)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert events[0]["rssiDbm"] == rssi
    assert events[0]["txPowerDbm"] == tx_power


@pytest.mark.asyncio
async def test_subsecond_future_skew_is_clamped_to_zero_age(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.999},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert events[0]["state"] == "observed"
    assert events[0]["ageMs"] == 0
    assert events[0]["receivedAt"] == "2026-07-21T12:00:00.123Z"


@pytest.mark.parametrize(
    ("advertisements", "timestamps", "advertisement_error", "timestamp_error", "error_name"),
    [
        ({}, {}, AttributeError("private-error-sentinel"), None, "AttributeError"),
        ({}, {}, NotImplementedError("private-error-sentinel"), None, "NotImplementedError"),
        ({}, {}, RuntimeError("private-error-sentinel"), None, "RuntimeError"),
        ({}, {}, None, AttributeError("private-error-sentinel"), "AttributeError"),
        ([], {}, None, None, "TypeError"),
        ({}, [], None, None, "TypeError"),
        (FaultyMapping(), {}, None, None, "KeyError"),
    ],
)
@pytest.mark.asyncio
async def test_source_cache_fault_is_isolated_and_logs_only_exception_class(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
    advertisements: object,
    timestamps: object,
    advertisement_error: BaseException | None,
    timestamp_error: BaseException | None,
    error_name: str,
) -> None:
    bad = FakeScanner(
        "scanner-bad",
        connectable=False,
        advertisements=advertisements,
        timestamps=timestamps,
        advertisement_error=advertisement_error,
        timestamp_error=timestamp_error,
    )
    good = FakeScanner(
        "scanner-good",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.extend([bad, good])
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(
        _spec(sources=("scanner-bad", "scanner-good")), events.append
    )

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [event["state"] for event in events] == ["stale", "observed"]
    assert [event["scannerSource"] for event in events] == ["scanner-bad", "scanner-good"]
    assert error_name in caplog.text
    assert "private-error-sentinel" not in caplog.text
    assert "private-key-fault-sentinel" not in caplog.text
    assert "private" not in " ".join(str(value) for event in events for value in event.values())


@pytest.mark.asyncio
async def test_mismatched_mapping_keys_use_only_their_intersection_without_fault_log(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={
            ADDRESS_ONE: _cache_entry(ADDRESS_ONE),
            ADDRESS_TWO: _cache_entry(ADDRESS_TWO),
        },
        timestamps={ADDRESS_ONE: 100.0, "AA:BB:CC:DD:EE:03": 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [event["state"] for event in events] == ["observed"]
    assert "scanner cache unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_disjoint_mapping_keys_emit_initial_stale_without_source_fault(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_TWO: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [event["state"] for event in events] == ["stale"]
    assert "scanner cache unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_non_string_mapping_key_fault_is_isolated_to_source(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={7: _cache_entry(ADDRESS_ONE)},
        timestamps={},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [event["state"] for event in events] == ["stale"]
    assert "TypeError" in caplog.text


@pytest.mark.asyncio
async def test_initial_events_are_tracker_then_source_order_from_one_shared_snapshot(
    harness: RuntimeHarness,
) -> None:
    private_name = "private-local-name-sentinel"
    advertisements = {
        ADDRESS_ONE: _cache_entry(ADDRESS_ONE, rssi=-41, private_name=private_name),
        ADDRESS_TWO: _cache_entry(ADDRESS_TWO, rssi=-72, private_name=private_name),
    }
    timestamps = {ADDRESS_ONE: 99.75, ADDRESS_TWO: 99.5}
    nonconnectable = FakeScanner(
        "scanner-nonconnectable",
        connectable=False,
        advertisements=advertisements,
        timestamps=timestamps,
    )
    connectable = FakeScanner(
        "scanner-connectable",
        connectable=True,
        advertisements=advertisements,
        timestamps=timestamps,
    )
    harness.scanners.extend([connectable, nonconnectable])
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    spec = _spec(
        trackers=(_tracker(), _tracker(TRACKER_TWO_ID, ADDRESS_TWO)),
        sources=("scanner-nonconnectable", "scanner-connectable"),
    )
    subscription = await sampler.async_subscribe(spec, events.append)

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [(event["trackerId"], event["scannerSource"]) for event in events] == [
        (str(TRACKER_ONE_ID), "scanner-nonconnectable"),
        (str(TRACKER_ONE_ID), "scanner-connectable"),
        (str(TRACKER_TWO_ID), "scanner-nonconnectable"),
        (str(TRACKER_TWO_ID), "scanner-connectable"),
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert len({event["streamId"] for event in events}) == 1
    assert nonconnectable.advertisement_reads == nonconnectable.timestamp_reads == 1
    assert connectable.advertisement_reads == connectable.timestamp_reads == 1
    assert harness.clock.monotonic_calls == 1
    assert harness.clock.wall_calls == 1
    wire_text = repr(events)
    for private_value in (
        ADDRESS_ONE,
        ADDRESS_TWO,
        private_name,
        "private-manufacturer-bytes",
        "private-service-bytes",
        "private-service-uuid",
        "private-platform-data",
    ):
        assert private_value not in wire_text


@pytest.mark.asyncio
async def test_new_subscriber_is_inactive_until_next_tick_after_emit_initial(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []

    subscription = await sampler.async_subscribe(_spec(), events.append)
    await harness.sleeper.release()

    assert events == []
    assert scanner.advertisement_reads == scanner.timestamp_reads == 0

    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert [event["state"] for event in events] == ["observed"]
    assert scanner.advertisement_reads == scanner.timestamp_reads == 1


@pytest.mark.asyncio
async def test_unchanged_regressed_advanced_and_strict_stale_transitions(
    harness: RuntimeHarness,
) -> None:
    advertisements = {ADDRESS_ONE: _cache_entry(ADDRESS_ONE, rssi=-55)}
    timestamps = {ADDRESS_ONE: 100.0}
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements=advertisements,
        timestamps=timestamps,
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    harness.clock.monotonic = 101.0
    await harness.sleeper.release()
    timestamps[ADDRESS_ONE] = 99.0
    harness.clock.monotonic = 102.0
    await harness.sleeper.release()
    timestamps[ADDRESS_ONE] = 102.0
    await harness.sleeper.release()
    harness.clock.monotonic = 112.0
    await harness.sleeper.release()
    harness.clock.monotonic = 112.001
    await harness.sleeper.release()
    harness.clock.monotonic = 113.0
    await harness.sleeper.release()

    assert [event["state"] for event in events] == ["observed", "observed", "stale"]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[2]["ageMs"] == 10_001


@pytest.mark.asyncio
async def test_source_removal_is_immediate_and_same_fresh_timestamp_restores_observed(
    harness: RuntimeHarness,
) -> None:
    advertisements = {ADDRESS_ONE: _cache_entry(ADDRESS_ONE)}
    timestamps = {ADDRESS_ONE: 100.0}
    scanner = FakeScanner(
        "scanner-one",
        connectable=False,
        advertisements=advertisements,
        timestamps=timestamps,
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    harness.clock.monotonic = 100.5
    harness.scanners.clear()
    await harness.sleeper.release()
    harness.clock.monotonic = 100.75
    harness.scanners.append(scanner)
    await harness.sleeper.release()
    advertisements.clear()
    timestamps.clear()
    harness.clock.monotonic = 101.0
    await harness.sleeper.release()
    harness.clock.monotonic = 110.751
    await harness.sleeper.release()

    assert [event["state"] for event in events] == [
        "observed",
        "stale",
        "observed",
        "stale",
    ]
    assert events[1]["ageMs"] == 500
    assert events[2]["ageMs"] == 750
    assert events[3]["ageMs"] == 10_751


@pytest.mark.asyncio
async def test_source_getter_fault_ages_normally_instead_of_immediate_removal(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    scanner.advertisement_error = RuntimeError("private-cache-sentinel")
    harness.clock.monotonic = 100.5
    await harness.sleeper.release()
    harness.clock.monotonic = 101.0
    await harness.sleeper.release()
    harness.clock.monotonic = 110.001
    await harness.sleeper.release()

    assert [event["state"] for event in events] == ["observed", "stale"]
    assert events[1]["ageMs"] == 10_001
    assert caplog.text.count("scanner cache unavailable") == 1

    scanner.advertisement_error = None
    harness.clock.monotonic = 110.25
    cast(dict[str, float], scanner.timestamps)[ADDRESS_ONE] = 110.25
    await harness.sleeper.release()
    scanner.advertisement_error = RuntimeError("private-cache-sentinel")
    harness.clock.monotonic = 110.5
    await harness.sleeper.release()

    assert caplog.text.count("scanner cache unavailable") == 2


@pytest.mark.asyncio
async def test_duplicate_enumeration_fault_is_not_removal_and_is_logged_once_per_episode(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    duplicate = FakeScanner("scanner-one", connectable=False)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    harness.scanners.append(duplicate)
    harness.clock.monotonic = 100.5
    await harness.sleeper.release()
    harness.clock.monotonic = 101.0
    await harness.sleeper.release()

    assert [event["state"] for event in events] == ["observed"]
    assert caplog.text.count("scanner enumeration unavailable") == 1

    harness.scanners.remove(duplicate)
    await harness.sleeper.release()
    harness.scanners.append(duplicate)
    await harness.sleeper.release()

    assert caplog.text.count("scanner enumeration unavailable") == 2


@pytest.mark.asyncio
async def test_old_initial_entry_is_stale_then_fresh_advance_is_observed(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 89.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    cast(dict[str, float], scanner.timestamps)[ADDRESS_ONE] = 100.0
    await harness.sleeper.release()

    assert [event["state"] for event in events] == ["stale", "observed"]
    assert events[0]["ageMs"] == 10_000


@pytest.mark.asyncio
async def test_freshness_uses_monotonic_time_and_age_is_bounded_to_one_day(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 99.5},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    assert events[0]["receivedAt"] == "2026-07-21T11:59:59.623Z"
    harness.clock.wall = datetime(2000, 1, 1, tzinfo=UTC)
    harness.clock.monotonic = 86_500.0
    await harness.sleeper.release()

    assert [event["state"] for event in events] == ["observed", "stale"]
    assert events[1]["ageMs"] == 86_400_000


@pytest.mark.asyncio
async def test_infinite_monotonic_age_on_source_removal_is_bounded(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: -1e308},
    )
    harness.scanners.append(scanner)
    harness.clock.monotonic = -1e308
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial

    harness.clock.monotonic = 1e308
    harness.scanners.clear()
    await harness.sleeper.release()

    assert [event["state"] for event in events] == ["observed", "stale"]
    assert events[1]["ageMs"] == 86_400_000


@pytest.mark.asyncio
async def test_invalid_shared_monotonic_clock_fails_closed_without_events_or_retries(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    harness.clock.monotonic = math.nan
    sampler = harness.make_runtime()
    events: list[dict[str, object]] = []
    subscription = await sampler.async_subscribe(_spec(), events.append)
    initial = asyncio.create_task(subscription.async_emit_initial())

    await harness.sleeper.release()
    initial_result = await asyncio.gather(initial, return_exceptions=True)
    await asyncio.sleep(0)

    assert isinstance(initial_result[0], asyncio.CancelledError)
    assert events == []
    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert caplog.text.count("sampler tick failed") == 1
    assert harness.clock.monotonic_calls == 1
    assert scanner.advertisement_reads == scanner.timestamp_reads == 0
    with pytest.raises(harness.module.RuntimeUnavailableError):
        await sampler.async_subscribe(_spec(), events.append)


@pytest.mark.asyncio
async def test_shared_tick_reads_each_source_once_for_simultaneous_subscribers(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    first_events: list[dict[str, object]] = []
    second_events: list[dict[str, object]] = []
    first = await sampler.async_subscribe(_spec(), first_events.append)
    second = await sampler.async_subscribe(_spec(), second_events.append)

    first_initial = asyncio.create_task(first.async_emit_initial())
    second_initial = asyncio.create_task(second.async_emit_initial())
    await harness.sleeper.release()
    await asyncio.gather(first_initial, second_initial)

    assert len(first_events) == len(second_events) == 1
    assert first_events[0]["streamId"] != second_events[0]["streamId"]
    assert scanner.advertisement_reads == scanner.timestamp_reads == 1
    assert harness.clock.monotonic_calls == harness.clock.wall_calls == 1


@pytest.mark.asyncio
async def test_mid_cycle_prepared_subscriber_adds_no_reads_until_activated(
    harness: RuntimeHarness,
) -> None:
    first_scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    second_scanner = FakeScanner(
        "scanner-two",
        connectable=False,
        advertisements={ADDRESS_TWO: _cache_entry(ADDRESS_TWO)},
        timestamps={ADDRESS_TWO: 100.0},
    )
    harness.scanners.extend([first_scanner, second_scanner])
    sampler = harness.make_runtime()
    first = await sampler.async_subscribe(_spec(), _ignore_event)
    first_initial = asyncio.create_task(first.async_emit_initial())
    await harness.sleeper.release()
    await first_initial

    second_events: list[dict[str, object]] = []
    second = await sampler.async_subscribe(
        _spec(
            trackers=(_tracker(TRACKER_TWO_ID, ADDRESS_TWO),),
            sources=("scanner-two",),
        ),
        second_events.append,
    )
    await harness.sleeper.release()

    assert second_events == []
    assert second_scanner.advertisement_reads == second_scanner.timestamp_reads == 0

    second_initial = asyncio.create_task(second.async_emit_initial())
    await harness.sleeper.release()
    await second_initial

    assert len(second_events) == 1
    assert second_scanner.advertisement_reads == second_scanner.timestamp_reads == 1


@pytest.mark.asyncio
async def test_sampler_is_capped_by_one_interval_per_tick_without_catchup(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    initial = asyncio.create_task(subscription.async_emit_initial())

    await harness.sleeper.release()
    await initial
    calls_after_first = len(harness.sleeper.calls)
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(harness.sleeper.calls) == calls_after_first

    harness.clock.monotonic += 20.0
    await harness.sleeper.release()

    assert all(delay == 0.25 for delay in harness.sleeper.calls)
    assert scanner.advertisement_reads == scanner.timestamp_reads == 2


@pytest.mark.asyncio
async def test_callback_errors_and_cancelled_error_do_not_break_other_subscribers(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    healthy_events: list[dict[str, object]] = []

    def failing_callback(event: dict[str, object]) -> None:
        del event
        raise RuntimeError("private-callback-sentinel")

    def cancelled_callback(event: dict[str, object]) -> None:
        del event
        raise asyncio.CancelledError

    failing = await sampler.async_subscribe(_spec(), failing_callback)
    cancelled = await sampler.async_subscribe(_spec(), cancelled_callback)
    healthy = await sampler.async_subscribe(_spec(), healthy_events.append)
    initial_tasks = [
        asyncio.create_task(subscription.async_emit_initial())
        for subscription in (failing, cancelled, healthy)
    ]
    await harness.sleeper.release()
    initial_results = await asyncio.gather(*initial_tasks, return_exceptions=True)
    cast(dict[str, float], scanner.timestamps)[ADDRESS_ONE] = 100.25
    harness.clock.monotonic = 100.25
    await harness.sleeper.release()

    assert isinstance(initial_results[0], asyncio.CancelledError)
    assert isinstance(initial_results[1], asyncio.CancelledError)
    assert initial_results[2] is None
    assert [event["sequence"] for event in healthy_events] == [1, 2]
    assert caplog.text.count("RuntimeError") == 1
    assert caplog.text.count("CancelledError") == 1
    assert "private-callback-sentinel" not in caplog.text
    assert sampler.subscription_count == 1
    assert sampler.retained_identity_count == 1
    assert failing.retains_identity is False
    assert cancelled.retains_identity is False
    assert healthy.retains_identity is True


@pytest.mark.asyncio
async def test_cancel_during_fanout_skips_cancelled_subscriber_and_loop_survives(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    cancelled_events: list[dict[str, object]] = []
    survivor_events: list[dict[str, object]] = []
    cancelled_holder: dict[str, Any] = {}

    def cancel_other(event: dict[str, object]) -> None:
        survivor_events.append(event)
        cancelled_holder["subscription"].cancel()

    survivor = await sampler.async_subscribe(_spec(), cancel_other)
    cancelled = await sampler.async_subscribe(_spec(), cancelled_events.append)
    cancelled_holder["subscription"] = cancelled
    cancelled_initial = asyncio.create_task(cancelled.async_emit_initial())
    survivor_initial = asyncio.create_task(survivor.async_emit_initial())
    await harness.sleeper.release()
    await asyncio.gather(cancelled_initial, survivor_initial, return_exceptions=True)
    cast(dict[str, float], scanner.timestamps)[ADDRESS_ONE] = 100.25
    harness.clock.monotonic = 100.25
    await harness.sleeper.release()

    assert cancelled_events == []
    assert [event["sequence"] for event in survivor_events] == [1, 2]


@pytest.mark.asyncio
async def test_atomic_global_subscription_limit_and_idempotent_slot_release(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()

    results = await asyncio.gather(
        *(sampler.async_subscribe(_spec(), _ignore_event) for _ in range(MAX_SUBSCRIPTIONS + 1)),
        return_exceptions=True,
    )

    subscriptions = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(subscriptions) == MAX_SUBSCRIPTIONS
    assert len(failures) == 1
    assert isinstance(failures[0], harness.module.TooManySubscriptionsError)

    retained_cancel = subscriptions[0].cancel
    retained_cancel()
    retained_cancel()
    replacement = await sampler.async_subscribe(_spec(), _ignore_event)
    await sampler.async_close()
    retained_cancel()
    replacement.cancel()

    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0


@pytest.mark.asyncio
async def test_sampler_task_is_registered_as_home_assistant_background_work(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    calls: list[str] = []
    original = HomeAssistant.async_create_background_task

    def recording_create_background_task(
        hass: HomeAssistant,
        target: Any,
        name: str,
        eager_start: bool = True,
    ) -> asyncio.Task[Any]:
        calls.append(name)
        return original(hass, target, name, eager_start)

    monkeypatch.setattr(
        HomeAssistant,
        "async_create_background_task",
        recording_create_background_task,
    )
    sampler = harness.make_runtime()

    subscription = await sampler.async_subscribe(_spec(), _ignore_event)

    assert calls == ["joyful_ble_positioning_sampler"]
    subscription.cancel()


@pytest.mark.asyncio
async def test_task_creation_failure_rolls_back_registration_and_closes_coroutine(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    captured_coroutines: list[Any] = []

    def failing_create_background_task(
        hass: HomeAssistant,
        target: Any,
        name: str,
        eager_start: bool = True,
    ) -> asyncio.Task[Any]:
        del hass, name, eager_start
        captured_coroutines.append(target)
        raise RuntimeError("private-task-creation-sentinel")

    monkeypatch.setattr(
        HomeAssistant,
        "async_create_background_task",
        failing_create_background_task,
    )
    sampler = harness.make_runtime()

    with pytest.raises(harness.module.RuntimeUnavailableError) as raised:
        await sampler.async_subscribe(_spec(), _ignore_event)

    assert "private-task-creation-sentinel" not in str(raised.value)
    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert len(captured_coroutines) == 1
    assert captured_coroutines[0].cr_frame is None


@pytest.mark.asyncio
async def test_task_creation_cancellation_rolls_back_and_preserves_cancellation(
    harness: RuntimeHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    captured_coroutines: list[Any] = []

    def cancelling_create_background_task(
        hass: HomeAssistant,
        target: Any,
        name: str,
        eager_start: bool = True,
    ) -> asyncio.Task[Any]:
        del hass, name, eager_start
        captured_coroutines.append(target)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        HomeAssistant,
        "async_create_background_task",
        cancelling_create_background_task,
    )
    sampler = harness.make_runtime()

    with pytest.raises(asyncio.CancelledError):
        await sampler.async_subscribe(_spec(), _ignore_event)

    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert len(captured_coroutines) == 1
    assert captured_coroutines[0].cr_frame is None


@pytest.mark.asyncio
async def test_external_sampler_cancellation_fails_closed_and_wipes_subscriber(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await asyncio.sleep(0)
    sampler_task = next(iter(sampler._sampler_tasks))

    sampler_task.cancel()
    await asyncio.gather(sampler_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert initial.done()
    assert initial.cancelled()
    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert subscription.retains_identity is False
    with pytest.raises(harness.module.RuntimeUnavailableError):
        await sampler.async_subscribe(_spec(), _ignore_event)


@pytest.mark.asyncio
async def test_sampler_sleep_failure_fails_closed_and_wipes_subscriber(
    harness: RuntimeHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    release_failure = asyncio.Event()

    async def failing_sleep(delay: float) -> None:
        assert delay == 0.25
        await release_failure.wait()
        raise RuntimeError("private-sleep-failure-sentinel")

    sampler = harness.module.BleObservationRuntime(harness.hass, sleep=failing_sleep)
    harness.runtimes.append(sampler)
    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await asyncio.sleep(0)
    sampler_task = next(iter(sampler._sampler_tasks))

    release_failure.set()
    await asyncio.gather(sampler_task, return_exceptions=True)
    await asyncio.sleep(0)

    assert initial.cancelled()
    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert subscription.retains_identity is False
    assert caplog.text.count("sampler tick failed") == 1
    assert "private-sleep-failure-sentinel" not in caplog.text
    with pytest.raises(harness.module.RuntimeUnavailableError):
        await sampler.async_subscribe(_spec(), _ignore_event)


@pytest.mark.asyncio
async def test_cancelling_initial_wait_erases_subscription_and_stops_sampler(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await asyncio.sleep(0)

    initial.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initial
    await asyncio.sleep(0)

    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert subscription.retains_identity is False


@pytest.mark.asyncio
async def test_cancel_and_close_erase_identity_state_even_if_bound_cancel_is_retained(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    retained_cancel = subscription.cancel

    assert subscription.retains_identity is True

    retained_cancel()
    await asyncio.sleep(0)

    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert subscription.retains_identity is False
    assert ADDRESS_ONE not in repr(sampler)
    assert ADDRESS_ONE not in repr(subscription)
    assert ADDRESS_ONE not in repr(retained_cancel)

    replacement = await sampler.async_subscribe(_spec(), _ignore_event)
    retained_replacement_cancel = replacement.cancel
    assert replacement.retains_identity is True
    await sampler.async_close()

    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert replacement.retains_identity is False
    assert ADDRESS_ONE not in repr(replacement)
    assert ADDRESS_ONE not in repr(retained_replacement_cancel)
    with pytest.raises(harness.module.RuntimeUnavailableError):
        await sampler.async_subscribe(_spec(), _ignore_event)


@pytest.mark.asyncio
async def test_rapid_final_cancel_and_resubscribe_uses_fresh_stream_and_sequence(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements={ADDRESS_ONE: _cache_entry(ADDRESS_ONE)},
        timestamps={ADDRESS_ONE: 100.0},
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    first_events: list[dict[str, object]] = []
    first = await sampler.async_subscribe(_spec(), first_events.append)
    first_initial = asyncio.create_task(first.async_emit_initial())
    await harness.sleeper.release()
    await first_initial
    first.cancel()

    second_events: list[dict[str, object]] = []
    second = await sampler.async_subscribe(_spec(), second_events.append)
    second_initial = asyncio.create_task(second.async_emit_initial())
    await harness.sleeper.release()
    await second_initial

    assert first_events[0]["sequence"] == second_events[0]["sequence"] == 1
    assert first_events[0]["streamId"] != second_events[0]["streamId"]


@pytest.mark.asyncio
async def test_no_work_without_subscriptions_and_last_cancel_stops_and_erases(
    harness: RuntimeHarness,
) -> None:
    scanner = FakeScanner("scanner-one", connectable=True)
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    await asyncio.sleep(0)

    assert harness.current_calls == 0
    assert harness.sleeper.calls == []

    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    assert sampler.subscription_count == 1
    subscription.cancel()
    await asyncio.sleep(0)

    assert sampler.subscription_count == 0
    assert sampler.retained_identity_count == 0
    assert sampler.sampler_running is False
    assert scanner.advertisement_reads == scanner.timestamp_reads == 0


@pytest.mark.asyncio
async def test_tick_releases_scanner_and_raw_mapping_references(
    harness: RuntimeHarness,
) -> None:
    advertisement_refs: list[weakref.ReferenceType[WeakMapping]] = []
    timestamp_refs: list[weakref.ReferenceType[WeakMapping]] = []

    def advertisements() -> WeakMapping:
        mapping = WeakMapping({ADDRESS_ONE: _cache_entry(ADDRESS_ONE)})
        advertisement_refs.append(weakref.ref(mapping))
        return mapping

    def timestamps() -> WeakMapping:
        mapping = WeakMapping({ADDRESS_ONE: 100.0})
        timestamp_refs.append(weakref.ref(mapping))
        return mapping

    scanner = FakeScanner(
        "scanner-one",
        connectable=True,
        advertisements=advertisements,
        timestamps=timestamps,
    )
    harness.scanners.append(scanner)
    sampler = harness.make_runtime()
    subscription = await sampler.async_subscribe(_spec(), _ignore_event)
    initial = asyncio.create_task(subscription.async_emit_initial())
    await harness.sleeper.release()
    await initial
    gc.collect()

    assert advertisement_refs and advertisement_refs[0]() is None
    assert timestamp_refs and timestamp_refs[0]() is None
