"""One shared, freshness-bounded sampler over public BLE scanner caches."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypedDict, cast, overload
from uuid import UUID, uuid4

import bluetooth_data_tools
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BaseHaScanner
from homeassistant.components import bluetooth as ha_bluetooth
from homeassistant.core import HomeAssistant

from .const import MAX_SUBSCRIPTIONS, SAMPLE_INTERVAL_SECONDS, STALE_AFTER_SECONDS
from .matcher import MatchResult, match_tracker
from .model import SubscriptionSpec, TrackerSpec

_LOGGER = logging.getLogger(__name__)

_MAX_AGE_MS = 86_400_000
_FUTURE_SKEW_SECONDS = 1.0

type CurrentScannersApi = Callable[[HomeAssistant], list[BaseHaScanner]]
type ScannerBySourceApi = Callable[[HomeAssistant, str], BaseHaScanner | None]
type Sleep = Callable[[float], Awaitable[None]]
type CreateBackgroundTask = Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]]


class IncompatibleBluetoothApiError(RuntimeError):
    """Raised when the pinned public Bluetooth API contract is unavailable."""


class TooManySubscriptionsError(RuntimeError):
    """Raised when the process-wide subscription bound is reached."""


class RuntimeUnavailableError(RuntimeError):
    """Raised when a closed runtime cannot accept or activate a subscription."""


class ScannerSourceUnavailableError(RuntimeError):
    """Raised when a requested scanner source is not currently available."""


class _InvalidClockError(RuntimeError):
    """Raised when the shared tick clock cannot provide a finite instant."""


class ObservedWireEvent(TypedDict):
    """Exact observed-event transport shape."""

    streamId: str
    state: Literal["observed"]
    trackerId: str
    scannerSource: str
    sequence: int
    rssiDbm: int
    txPowerDbm: int | None
    ageMs: int
    receivedAt: str


class StaleWireEvent(TypedDict):
    """Exact stale-event transport shape."""

    streamId: str
    state: Literal["stale"]
    trackerId: str
    scannerSource: str
    sequence: int
    ageMs: int


type WireEvent = ObservedWireEvent | StaleWireEvent
type EventCallback = Callable[[WireEvent], None]


def _discard_event(event: WireEvent) -> None:
    del event


_EMPTY_SUBSCRIPTION_SPEC = SubscriptionSpec(trackers=(), scanner_sources=())


@dataclass(frozen=True, slots=True)
class ObservedEvent:
    """A minimized fresh observation."""

    stream_id: str
    state: Literal["observed"]
    tracker_id: str
    scanner_source: str
    sequence: int
    rssi_dbm: int
    tx_power_dbm: int | None
    age_ms: int
    received_at: str


@dataclass(frozen=True, slots=True)
class StaleEvent:
    """A minimized stale transition."""

    stream_id: str
    state: Literal["stale"]
    tracker_id: str
    scanner_source: str
    sequence: int
    age_ms: int


@overload
def as_wire_event(event: ObservedEvent) -> ObservedWireEvent: ...


@overload
def as_wire_event(event: StaleEvent) -> StaleWireEvent: ...


def as_wire_event(event: ObservedEvent | StaleEvent) -> WireEvent:
    """Serialize only the explicit transport fields."""
    if isinstance(event, ObservedEvent):
        return {
            "streamId": event.stream_id,
            "state": event.state,
            "trackerId": event.tracker_id,
            "scannerSource": event.scanner_source,
            "sequence": event.sequence,
            "rssiDbm": event.rssi_dbm,
            "txPowerDbm": event.tx_power_dbm,
            "ageMs": event.age_ms,
            "receivedAt": event.received_at,
        }
    return {
        "streamId": event.stream_id,
        "state": event.state,
        "trackerId": event.tracker_id,
        "scannerSource": event.scanner_source,
        "sequence": event.sequence,
        "ageMs": event.age_ms,
    }


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_api_error(reason: str) -> IncompatibleBluetoothApiError:
    return IncompatibleBluetoothApiError(reason)


def _public_api_functions() -> tuple[CurrentScannersApi, ScannerBySourceApi]:
    expected = {
        "async_current_scanners": ("hass",),
        "async_scanner_by_source": ("hass", "source"),
    }
    resolved: dict[str, object] = {}
    for name, parameter_names in expected.items():
        function = getattr(ha_bluetooth, name, None)
        if not callable(function):
            raise _safe_api_error("missing public bluetooth capability")
        try:
            parameters = tuple(inspect.signature(function).parameters.values())
        except TypeError, ValueError:
            raise _safe_api_error("invalid public bluetooth capability") from None
        if tuple(parameter.name for parameter in parameters) != parameter_names or any(
            parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
            or parameter.default is not inspect.Parameter.empty
            for parameter in parameters
        ):
            raise _safe_api_error("invalid public bluetooth capability")
        resolved[name] = function
    return (
        cast(CurrentScannersApi, resolved["async_current_scanners"]),
        cast(ScannerBySourceApi, resolved["async_scanner_by_source"]),
    )


def current_scanners(hass: HomeAssistant) -> dict[str, BaseHaScanner]:
    """Return one fail-closed source-indexed public scanner snapshot."""
    current_function, _ = _public_api_functions()
    try:
        current = current_function(hass)
    except Exception as err:
        raise _safe_api_error(f"scanner enumeration failed ({type(err).__name__})") from None
    if not isinstance(current, list):
        raise _safe_api_error("invalid scanner enumeration")

    scanners: dict[str, BaseHaScanner] = {}
    try:
        for scanner in current:
            if not isinstance(scanner, BaseHaScanner):
                raise _safe_api_error("invalid scanner enumeration")
            source = scanner.source
            if not isinstance(source, str) or not source:
                raise _safe_api_error("invalid scanner source")
            if source in scanners:
                raise _safe_api_error("duplicate scanner source")
            scanners[source] = scanner
    except IncompatibleBluetoothApiError:
        raise
    except Exception as err:
        raise _safe_api_error(f"invalid scanner enumeration ({type(err).__name__})") from None
    return scanners


def capability_check(hass: HomeAssistant) -> None:
    """Fail closed unless the exact pinned public scanner API is usable."""
    current_scanners(hass)


@dataclass(slots=True)
class _PairState:
    last_timestamp: float | None = None
    last_rssi: int | None = None
    last_tx_power: int | None = None
    emitted_state: Literal["observed", "stale"] | None = None


@dataclass(slots=True, repr=False)
class _Subscriber:
    spec: SubscriptionSpec
    callback: EventCallback
    stream_id: str
    active: bool = False
    sequence: int = 0
    initial_waiter: asyncio.Future[None] | None = None
    pairs: dict[tuple[UUID, str], _PairState] = field(default_factory=dict)

    def wipe(self) -> None:
        """Erase identity, callback, stream, and pair state in place."""
        self.spec = _EMPTY_SUBSCRIPTION_SPEC
        self.callback = _discard_event
        self.stream_id = ""
        self.active = False
        self.sequence = 0
        self.pairs.clear()
        if self.initial_waiter is not None and not self.initial_waiter.done():
            self.initial_waiter.cancel()


@dataclass(frozen=True, slots=True)
class _SourceSample:
    state: Literal["available", "fault", "removed"]
    matches: Mapping[TrackerSpec, MatchResult | None] = field(default_factory=dict)


class Subscription:
    """An idempotent, identity-free handle for one runtime registration."""

    __slots__ = ("_registration", "_runtime", "_token")

    def __init__(
        self,
        runtime: BleObservationRuntime,
        token: UUID,
        registration: _Subscriber,
    ) -> None:
        self._runtime: BleObservationRuntime | None = runtime
        self._token: UUID | None = token
        self._registration = registration

    def __repr__(self) -> str:
        return f"<Subscription active={self._runtime is not None}>"

    @property
    def retains_identity(self) -> bool:
        """Report whether the retained registration still owns sensitive state."""
        registration = self._registration
        return bool(
            registration.spec.trackers
            or registration.spec.scanner_sources
            or registration.pairs
            or registration.callback is not _discard_event
        )

    def cancel(self) -> None:
        """Release the registration exactly once and erase this handle."""
        runtime = self._runtime
        token = self._token
        self._runtime = None
        self._token = None
        if runtime is not None and token is not None:
            runtime._cancel_subscription(token)

    async def async_emit_initial(self) -> None:
        """Activate and await the next ordinary sampler tick."""
        runtime = self._runtime
        token = self._token
        if runtime is None or token is None:
            raise RuntimeUnavailableError("subscription unavailable")
        try:
            await runtime._async_activate(token)
        except asyncio.CancelledError:
            self.cancel()
            raise


class BleObservationRuntime:
    """Own one bounded subscription set and one shared scheduled sampler."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        sleep: Sleep = asyncio.sleep,
        create_background_task: CreateBackgroundTask | None = None,
    ) -> None:
        self._hass = hass
        self._sleep = sleep
        self._create_background_task = create_background_task or hass.async_create_background_task
        self._subscriptions: dict[UUID, _Subscriber] = {}
        self._sampler_task: asyncio.Task[None] | None = None
        self._sampler_tasks: set[asyncio.Task[None]] = set()
        self._active_faults: set[tuple[str, str]] = set()
        self._closed = False

    def __repr__(self) -> str:
        return (
            "<BleObservationRuntime "
            f"closed={self._closed} subscriptions={len(self._subscriptions)}>"
        )

    @property
    def subscription_count(self) -> int:
        """Return a privacy-safe active registration count."""
        return len(self._subscriptions)

    @property
    def retained_identity_count(self) -> int:
        """Return how many identity specs remain held in runtime memory."""
        return sum(len(subscriber.spec.trackers) for subscriber in self._subscriptions.values())

    @property
    def sampler_running(self) -> bool:
        """Return whether the current sampler task is live."""
        return self._sampler_task is not None and not self._sampler_task.done()

    async def async_subscribe(
        self,
        spec: SubscriptionSpec,
        on_event: EventCallback,
    ) -> Subscription:
        """Atomically prepare one inactive subscription without reading caches."""
        if self._closed:
            raise RuntimeUnavailableError("runtime unavailable")
        if len(self._subscriptions) >= MAX_SUBSCRIPTIONS:
            raise TooManySubscriptionsError("subscription limit reached")
        if not callable(on_event):
            raise TypeError("event callback must be callable")

        scanners = current_scanners(self._hass)
        _, scanner_by_source = _public_api_functions()
        for source in spec.scanner_sources:
            expected_scanner = scanners.get(source)
            if expected_scanner is None:
                raise ScannerSourceUnavailableError("scanner source unavailable")
            try:
                resolved_scanner = scanner_by_source(self._hass, source)
                resolved_source = resolved_scanner.source if resolved_scanner is not None else None
            except Exception as err:
                raise _safe_api_error(
                    f"scanner source lookup failed ({type(err).__name__})"
                ) from None
            if not isinstance(resolved_scanner, BaseHaScanner) or resolved_source != source:
                raise ScannerSourceUnavailableError("scanner source unavailable")

        token = uuid4()
        subscriber = _Subscriber(
            spec=spec,
            callback=on_event,
            stream_id=str(uuid4()),
        )
        self._subscriptions[token] = subscriber
        try:
            self._ensure_sampler()
        except BaseException:
            self._subscriptions.pop(token, None)
            subscriber.wipe()
            raise
        return Subscription(self, token, subscriber)

    def _ensure_sampler(self) -> None:
        if self._sampler_task is not None and not self._sampler_task.done():
            return
        sampler_coroutine = self._async_sampler_loop()
        try:
            task = self._create_background_task(
                sampler_coroutine,
                "joyful_ble_positioning_sampler",
            )
        except asyncio.CancelledError:
            sampler_coroutine.close()
            raise
        except Exception:
            sampler_coroutine.close()
            raise RuntimeUnavailableError("sampler unavailable") from None
        self._sampler_task = task
        self._sampler_tasks.add(task)
        task.add_done_callback(self._sampler_tasks.discard)

    async def _async_activate(self, token: UUID) -> None:
        if self._closed:
            raise RuntimeUnavailableError("runtime unavailable")
        subscriber = self._subscriptions.get(token)
        if subscriber is None:
            raise RuntimeUnavailableError("subscription unavailable")
        if subscriber.initial_waiter is None:
            subscriber.initial_waiter = asyncio.get_running_loop().create_future()
            subscriber.active = True
        await asyncio.shield(subscriber.initial_waiter)

    def _cancel_subscription(self, token: UUID) -> None:
        subscriber = self._subscriptions.pop(token, None)
        if subscriber is None:
            return
        subscriber.wipe()
        if self._subscriptions:
            return
        self._active_faults.clear()
        task = self._sampler_task
        self._sampler_task = None
        if task is not None and not task.done():
            task.cancel()

    async def async_close(self) -> None:
        """Stop sampling, release slots, and erase all identity-bearing state."""
        self._closed = True
        self._create_background_task = self._hass.async_create_background_task
        for token in tuple(self._subscriptions):
            self._cancel_subscription(token)
        tasks = tuple(self._sampler_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sampler_task = None
        self._active_faults.clear()

    async def _async_sampler_loop(self) -> None:
        current_task = asyncio.current_task()
        try:
            while self._subscriptions and not self._closed:
                await self._sleep(SAMPLE_INTERVAL_SECONDS)
                if not self._subscriptions or self._closed:
                    break
                self._sample_once()
        except asyncio.CancelledError:
            if self._sampler_task is current_task and not self._closed:
                self._fail_closed()
            raise
        except Exception as err:
            _LOGGER.warning("sampler tick failed (%s)", type(err).__name__)
            self._fail_closed()
        finally:
            if self._sampler_task is current_task:
                self._sampler_task = None

    def _sample_once(self) -> None:
        active_items = tuple(
            (token, subscriber)
            for token, subscriber in self._subscriptions.items()
            if subscriber.active
        )
        if not active_items:
            return

        monotonic_value = bluetooth_data_tools.monotonic_time_coarse()
        if (
            isinstance(monotonic_value, bool)
            or not isinstance(monotonic_value, (int, float))
            or not math.isfinite(monotonic_value)
        ):
            raise _InvalidClockError
        monotonic_now = float(monotonic_value)
        wall_now = _utc_now()
        if not isinstance(wall_now, datetime) or wall_now.utcoffset() is None:
            raise _InvalidClockError
        requested_sources = tuple(
            dict.fromkeys(
                source
                for _, subscriber in active_items
                for source in subscriber.spec.scanner_sources
            )
        )
        unique_trackers = tuple(
            dict.fromkeys(
                tracker for _, subscriber in active_items for tracker in subscriber.spec.trackers
            )
        )
        samples = self._snapshot_sources(requested_sources, unique_trackers, monotonic_now)

        for token, subscriber in active_items:
            if self._subscriptions.get(token) is not subscriber or not subscriber.active:
                continue
            events: list[ObservedEvent | StaleEvent] = []
            for tracker in subscriber.spec.trackers:
                for source in subscriber.spec.scanner_sources:
                    event = self._pair_event(
                        subscriber,
                        tracker,
                        source,
                        samples[source],
                        monotonic_now,
                        wall_now,
                    )
                    if event is not None:
                        events.append(event)
            for event in events:
                if self._subscriptions.get(token) is not subscriber or not subscriber.active:
                    break
                try:
                    subscriber.callback(as_wire_event(event))
                except (Exception, asyncio.CancelledError) as err:
                    _LOGGER.warning("subscriber callback failed (%s)", type(err).__name__)
                    self._cancel_subscription(token)
                    break
            if (
                self._subscriptions.get(token) is subscriber
                and subscriber.initial_waiter is not None
                and not subscriber.initial_waiter.done()
            ):
                subscriber.initial_waiter.set_result(None)

    def _fail_closed(self) -> None:
        self._closed = True
        self._create_background_task = self._hass.async_create_background_task
        for token in tuple(self._subscriptions):
            self._cancel_subscription(token)

    def _snapshot_sources(
        self,
        sources: tuple[str, ...],
        trackers: tuple[TrackerSpec, ...],
        monotonic_now: float,
    ) -> dict[str, _SourceSample]:
        try:
            scanners = current_scanners(self._hass)
        except IncompatibleBluetoothApiError as err:
            error_name = type(err).__name__
            fault = ("enumeration", error_name)
            if fault not in self._active_faults:
                _LOGGER.warning("scanner enumeration unavailable (%s)", error_name)
            self._active_faults = {fault}
            return {source: _SourceSample("fault") for source in sources}

        samples: dict[str, _SourceSample] = {}
        active_faults: set[tuple[str, str]] = set()
        for source in sources:
            scanner = scanners.get(source)
            if scanner is None:
                samples[source] = _SourceSample("removed")
                continue
            try:
                advertisements = scanner.discovered_devices_and_advertisement_data
                timestamps = scanner.discovered_device_timestamps
                if not isinstance(advertisements, Mapping) or not isinstance(timestamps, Mapping):
                    raise TypeError
                advertisement_keys = set(advertisements)
                timestamp_keys = set(timestamps)
                if any(
                    not isinstance(address, str) for address in advertisement_keys | timestamp_keys
                ):
                    raise TypeError
                common_keys = advertisement_keys & timestamp_keys
                intersected_advertisements: dict[str, tuple[BLEDevice, AdvertisementData]] = {}
                intersected_timestamps: dict[str, float] = {}
                for address in common_keys:
                    timestamp = _valid_timestamp(timestamps[address], monotonic_now)
                    if timestamp is None:
                        continue
                    advertisement_entry = cast(
                        tuple[BLEDevice, AdvertisementData], advertisements[address]
                    )
                    if not _valid_rssi(advertisement_entry[1].rssi):
                        continue
                    intersected_advertisements[address] = advertisement_entry
                    intersected_timestamps[address] = timestamp
                source_matches = {
                    tracker: match_tracker(
                        tracker,
                        intersected_advertisements,
                        intersected_timestamps,
                    )
                    for tracker in trackers
                }
            except Exception as err:
                error_name = type(err).__name__
                fault = (source, error_name)
                active_faults.add(fault)
                if fault not in self._active_faults:
                    _LOGGER.warning("scanner cache unavailable (%s)", error_name)
                samples[source] = _SourceSample("fault")
            else:
                samples[source] = _SourceSample("available", source_matches)
        self._active_faults = active_faults
        return samples

    def _pair_event(
        self,
        subscriber: _Subscriber,
        tracker: TrackerSpec,
        source: str,
        sample: _SourceSample,
        monotonic_now: float,
        wall_now: datetime,
    ) -> ObservedEvent | StaleEvent | None:
        pair = subscriber.pairs.setdefault((tracker.tracker_id, source), _PairState())
        match = sample.matches.get(tracker) if sample.state == "available" else None
        valid_match = match if match is not None and _valid_rssi(match.rssi_dbm) else None

        timestamp_changed = False
        if valid_match is not None:
            timestamp = valid_match.monotonic_timestamp
            if pair.last_timestamp is None or timestamp >= pair.last_timestamp:
                timestamp_changed = pair.last_timestamp is None or timestamp > pair.last_timestamp
                pair.last_timestamp = timestamp
                pair.last_rssi = valid_match.rssi_dbm
                pair.last_tx_power = _valid_tx_power(valid_match.tx_power_dbm)
            else:
                valid_match = None

        age_seconds = (
            max(0.0, monotonic_now - pair.last_timestamp)
            if pair.last_timestamp is not None
            else STALE_AFTER_SECONDS
        )
        stale = age_seconds > STALE_AFTER_SECONDS

        if sample.state == "removed":
            if pair.emitted_state == "observed" or pair.emitted_state is None:
                return self._stale_event(
                    subscriber,
                    tracker,
                    source,
                    age_seconds if pair.last_timestamp is not None else STALE_AFTER_SECONDS,
                    pair,
                )
            return None

        if valid_match is not None and not stale:
            if timestamp_changed or pair.emitted_state != "observed":
                return self._observed_event(
                    subscriber,
                    tracker,
                    source,
                    age_seconds,
                    wall_now,
                    pair,
                )
            return None

        if pair.emitted_state is None:
            return self._stale_event(
                subscriber,
                tracker,
                source,
                STALE_AFTER_SECONDS,
                pair,
            )
        if pair.emitted_state == "observed" and stale:
            return self._stale_event(
                subscriber,
                tracker,
                source,
                age_seconds,
                pair,
            )
        return None

    @staticmethod
    def _next_sequence(subscriber: _Subscriber) -> int:
        subscriber.sequence += 1
        return subscriber.sequence

    def _observed_event(
        self,
        subscriber: _Subscriber,
        tracker: TrackerSpec,
        source: str,
        age_seconds: float,
        wall_now: datetime,
        pair: _PairState,
    ) -> ObservedEvent:
        pair.emitted_state = "observed"
        return ObservedEvent(
            stream_id=subscriber.stream_id,
            state="observed",
            tracker_id=str(tracker.tracker_id),
            scanner_source=source,
            sequence=self._next_sequence(subscriber),
            rssi_dbm=cast(int, pair.last_rssi),
            tx_power_dbm=pair.last_tx_power,
            age_ms=_age_ms(age_seconds),
            received_at=_received_at(wall_now, age_seconds),
        )

    def _stale_event(
        self,
        subscriber: _Subscriber,
        tracker: TrackerSpec,
        source: str,
        age_seconds: float,
        pair: _PairState,
    ) -> StaleEvent:
        pair.emitted_state = "stale"
        return StaleEvent(
            stream_id=subscriber.stream_id,
            state="stale",
            tracker_id=str(tracker.tracker_id),
            scanner_source=source,
            sequence=self._next_sequence(subscriber),
            age_ms=_age_ms(age_seconds),
        )


def _valid_timestamp(value: object, monotonic_now: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        timestamp = float(value)
    except OverflowError:
        return None
    if not math.isfinite(timestamp) or timestamp - monotonic_now >= _FUTURE_SKEW_SECONDS:
        return None
    return timestamp


def _valid_rssi(value: object) -> bool:
    return type(value) is int and -126 <= value <= -1


def _valid_tx_power(value: object) -> int | None:
    return value if type(value) is int and -126 <= value <= 20 else None


def _age_ms(age_seconds: float) -> int:
    if not math.isfinite(age_seconds):
        return _MAX_AGE_MS if age_seconds > 0 else 0
    if age_seconds <= 0:
        return 0
    if age_seconds >= _MAX_AGE_MS / 1000:
        return _MAX_AGE_MS
    return round(age_seconds * 1000)


def _received_at(wall_now: datetime, age_seconds: float) -> str:
    received = wall_now.astimezone(UTC) - timedelta(seconds=age_seconds)
    return received.isoformat(timespec="milliseconds").replace("+00:00", "Z")
