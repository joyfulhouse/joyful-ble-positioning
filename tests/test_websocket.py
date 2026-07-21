"""WebSocket and config-entry lifecycle contracts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import messages
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components import joyful_ble_positioning as integration
from custom_components.joyful_ble_positioning.const import (
    DOMAIN,
    INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID,
    WS_TYPE,
)
from custom_components.joyful_ble_positioning.runtime import (
    IncompatibleBluetoothApiError,
    RuntimeUnavailableError,
    ScannerSourceUnavailableError,
    TooManySubscriptionsError,
)
from custom_components.joyful_ble_positioning.websocket import (
    DomainState,
    _async_subscribe_observations,
    get_loaded_runtime,
    websocket_subscribe_observations,
)

IDENTITY = "02:00:00:00:00:01"
TRACKER_ID = "00000000-0000-4000-8000-000000000001"
SOURCE = "scanner-one"
IBEACON_IDENTITY = "fda50693-a4e2-4fb1-afcf-c6eb07647825/1/27"
IBEACON_TRACKER_ID = "00000000-0000-4000-8000-000000000002"


def _message(**updates: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": 7,
        "type": WS_TYPE,
        "trackers": [
            {
                "trackerId": TRACKER_ID,
                "kind": "static-mac",
                "identity": IDENTITY,
            }
        ],
        "scannerSources": [SOURCE],
    }
    message.update(updates)
    return message


def _event(sequence: int = 1) -> dict[str, object]:
    return {
        "streamId": "00000000-0000-4000-8000-000000000099",
        "state": "observed",
        "trackerId": TRACKER_ID,
        "scannerSource": SOURCE,
        "sequence": sequence,
        "rssiDbm": -61,
        "txPowerDbm": None,
        "ageMs": 125,
        "receivedAt": "2026-07-21T12:00:00.000Z",
    }


class FakeConnection:
    """Small connection double that preserves response ordering."""

    def __init__(self, *, is_admin: bool = True) -> None:
        self.user = SimpleNamespace(is_admin=is_admin)
        self.subscriptions: dict[int, Callable[[], Any]] = {}
        self.calls: list[tuple[str, int, object | None]] = []

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.calls.append(("error", message_id, {"code": code, "message": message}))

    def send_result(self, message_id: int, result: object | None = None) -> None:
        self.calls.append(("result", message_id, result))

    def send_event(self, message_id: int, event: object | None = None) -> None:
        self.calls.append(("event", message_id, event))


class FakeSubscription:
    """Prepared runtime subscription with controllable initial delivery."""

    def __init__(
        self,
        *,
        initial_event: dict[str, object] | None = None,
        initial_error: BaseException | None = None,
        block_initial: bool = False,
    ) -> None:
        self.initial_event = initial_event
        self.initial_error = initial_error
        self.callback: Callable[[Any], None] | None = None
        self.cancel_calls = 0
        self.initial_started = asyncio.Event()
        self.initial_release = asyncio.Event()
        if not block_initial:
            self.initial_release.set()

    def cancel(self) -> None:
        self.cancel_calls += 1

    async def async_emit_initial(self) -> None:
        self.initial_started.set()
        await self.initial_release.wait()
        if self.initial_error is not None:
            raise self.initial_error
        if self.initial_event is not None:
            assert self.callback is not None
            self.callback(cast(Any, self.initial_event))


class FakeRuntime:
    """Runtime double that can yield or fail during preparation."""

    def __init__(
        self,
        subscription: FakeSubscription | None = None,
        *,
        prepare_error: BaseException | None = None,
        early_event: dict[str, object] | None = None,
        block_preparation: bool = False,
    ) -> None:
        self.subscription = subscription or FakeSubscription()
        self.prepare_error = prepare_error
        self.early_event = early_event
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block_preparation:
            self.release.set()
        self.subscribe_calls = 0
        self.close_calls = 0
        self.spec: object | None = None
        self.callback: Callable[[Any], None] | None = None

    async def async_subscribe(
        self,
        spec: object,
        callback: Callable[[Any], None],
    ) -> FakeSubscription:
        self.subscribe_calls += 1
        self.spec = spec
        self.callback = callback
        self.started.set()
        if self.early_event is not None:
            callback(cast(Any, self.early_event))
        await self.release.wait()
        if self.prepare_error is not None:
            raise self.prepare_error
        self.subscription.callback = callback
        return self.subscription

    async def async_close(self) -> None:
        self.close_calls += 1


class ClosableRuntime:
    """Runtime double whose close can be paused to prove detach ordering."""

    def __init__(self, *, block_close: bool = False) -> None:
        self.close_calls = 0
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        if not block_close:
            self.close_release.set()

    async def async_close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()


def _install_runtime(hass: HomeAssistant, runtime: object) -> DomainState:
    state = DomainState(runtime=cast(Any, runtime))
    hass.data[DOMAIN] = state
    return state


def test_framework_schema_is_neutral_until_admin_authorization() -> None:
    """The framework must not traverse identity input before require_admin."""
    assert websocket_subscribe_observations._ws_command == WS_TYPE
    schema = websocket_subscribe_observations._ws_schema
    malformed = {
        "id": 7,
        "type": WS_TYPE,
        "trackers": {"nested": IDENTITY},
        "scannerSources": None,
        "unexpected": [IDENTITY],
    }

    assert schema(malformed) == malformed


@pytest.mark.parametrize("user", [None, SimpleNamespace(is_admin=False)])
def test_authorization_precedes_parser_and_runtime(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    user: object | None,
) -> None:
    connection = FakeConnection()
    connection.user = cast(Any, user)
    parsed = False

    def forbidden_parse(message: object) -> object:
        nonlocal parsed
        parsed = True
        raise AssertionError(message)

    monkeypatch.setattr(
        "custom_components.joyful_ble_positioning.websocket.parse_subscription_spec",
        forbidden_parse,
    )

    with pytest.raises(Unauthorized):
        websocket_subscribe_observations(
            hass,
            cast(Any, connection),
            cast(Any, _message(trackers={"nested": IDENTITY})),
        )

    assert parsed is False
    assert connection.calls == []


@pytest.mark.asyncio
async def test_invalid_admin_payload_is_constant_and_never_reaches_runtime(
    hass: HomeAssistant,
) -> None:
    runtime = FakeRuntime()
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message(trackers={"nested": IDENTITY})),
    )

    assert runtime.subscribe_calls == 0
    assert connection.calls == [
        (
            "error",
            7,
            {
                "code": websocket_api.ERR_INVALID_FORMAT,
                "message": "Invalid BLE observation subscription.",
            },
        )
    ]
    assert IDENTITY not in repr(connection.calls)


@pytest.mark.asyncio
async def test_missing_runtime_is_fixed_not_supported(hass: HomeAssistant) -> None:
    hass.data[DOMAIN] = DomainState()
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )

    assert connection.calls == [
        (
            "error",
            7,
            {
                "code": websocket_api.ERR_NOT_SUPPORTED,
                "message": "BLE observation runtime is unavailable.",
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            ScannerSourceUnavailableError(f"missing {SOURCE}"),
            websocket_api.ERR_NOT_FOUND,
            "BLE scanner source was not found.",
        ),
        (
            TooManySubscriptionsError("private limit sentinel"),
            websocket_api.ERR_NOT_ALLOWED,
            "BLE observation subscription limit reached.",
        ),
        (
            IncompatibleBluetoothApiError("private API sentinel"),
            websocket_api.ERR_NOT_SUPPORTED,
            "BLE observation runtime is unavailable.",
        ),
        (
            RuntimeUnavailableError("private runtime sentinel"),
            websocket_api.ERR_NOT_SUPPORTED,
            "BLE observation runtime is unavailable.",
        ),
    ],
)
async def test_expected_preparation_errors_have_typed_constant_mapping(
    hass: HomeAssistant,
    error: BaseException,
    code: str,
    message: str,
) -> None:
    runtime = FakeRuntime(prepare_error=error)
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )

    assert connection.subscriptions == {}
    assert connection.calls == [("error", 7, {"code": code, "message": message})]
    serialized = repr(connection.calls)
    assert IDENTITY not in serialized
    assert SOURCE not in serialized
    assert "sentinel" not in serialized


@pytest.mark.asyncio
async def test_result_precedes_initial_event_and_early_event_is_dropped(
    hass: HomeAssistant,
) -> None:
    subscription = FakeSubscription(initial_event=_event(2))
    runtime = FakeRuntime(subscription, early_event=_event(1))
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )

    assert [call[0] for call in connection.calls] == ["result", "event"]
    assert connection.calls[1][2] == _event(2)
    assert 7 in connection.subscriptions


@pytest.mark.asyncio
async def test_disconnect_during_preparation_cancels_late_subscription_and_sends_nothing(
    hass: HomeAssistant,
) -> None:
    subscription = FakeSubscription(initial_event=_event())
    runtime = FakeRuntime(subscription, block_preparation=True)
    _install_runtime(hass, runtime)
    connection = FakeConnection()
    task = asyncio.create_task(
        _async_subscribe_observations(
            hass,
            cast(Any, connection),
            cast(Any, _message()),
        )
    )

    await runtime.started.wait()
    assert 7 in connection.subscriptions
    cleanup = connection.subscriptions.pop(7)
    cleanup()
    cleanup()
    runtime.release.set()
    await task

    assert subscription.cancel_calls == 1
    assert connection.calls == []


@pytest.mark.asyncio
async def test_runtime_replacement_during_preparation_cancels_and_returns_unavailable(
    hass: HomeAssistant,
) -> None:
    subscription = FakeSubscription()
    runtime = FakeRuntime(subscription, block_preparation=True)
    state = _install_runtime(hass, runtime)
    connection = FakeConnection()
    task = asyncio.create_task(
        _async_subscribe_observations(
            hass,
            cast(Any, connection),
            cast(Any, _message()),
        )
    )

    await runtime.started.wait()
    state.runtime = cast(Any, FakeRuntime())
    runtime.release.set()
    await task

    assert subscription.cancel_calls == 1
    assert connection.subscriptions == {}
    assert connection.calls == [
        (
            "error",
            7,
            {
                "code": websocket_api.ERR_NOT_SUPPORTED,
                "message": "BLE observation runtime is unavailable.",
            },
        )
    ]


@pytest.mark.asyncio
async def test_initial_failure_after_ack_cancels_without_second_result_or_secret_log(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    subscription = FakeSubscription(initial_error=RuntimeError("private initial sentinel"))
    runtime = FakeRuntime(subscription)
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )

    assert connection.calls == [("result", 7, None)]
    assert subscription.cancel_calls == 1
    assert "RuntimeError" in caplog.text
    assert "private initial sentinel" not in caplog.text
    assert IDENTITY not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_initial_delivery_is_silent_and_idempotent(
    hass: HomeAssistant,
) -> None:
    subscription = FakeSubscription(initial_error=asyncio.CancelledError())
    runtime = FakeRuntime(subscription)
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )

    assert connection.calls == [("result", 7, None)]
    assert connection.subscriptions == {}
    assert subscription.cancel_calls == 1


@pytest.mark.asyncio
async def test_external_task_cancellation_after_ack_cleans_up_and_propagates(
    hass: HomeAssistant,
) -> None:
    subscription = FakeSubscription(block_initial=True)
    runtime = FakeRuntime(subscription)
    _install_runtime(hass, runtime)
    connection = FakeConnection()
    task = asyncio.create_task(
        _async_subscribe_observations(
            hass,
            cast(Any, connection),
            cast(Any, _message()),
        )
    )

    await subscription.initial_started.wait()
    assert connection.calls == [("result", 7, None)]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection.subscriptions == {}
    assert subscription.cancel_calls == 1


@pytest.mark.asyncio
async def test_late_callback_after_unsubscribe_is_dropped(
    hass: HomeAssistant,
) -> None:
    runtime = FakeRuntime()
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )
    cleanup = connection.subscriptions.pop(7)
    cleanup()
    assert runtime.callback is not None
    runtime.callback(cast(Any, _event()))

    assert connection.calls == [("result", 7, None)]
    assert runtime.subscription.cancel_calls == 1


@pytest.mark.asyncio
async def test_callback_retains_only_connection_id_and_ack_state(
    hass: HomeAssistant,
) -> None:
    runtime = FakeRuntime()
    _install_runtime(hass, runtime)
    connection = FakeConnection()

    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )

    assert runtime.callback is not None
    closure_values = tuple(
        cell.cell_contents for cell in cast(Any, runtime.callback).__closure__ or ()
    )
    closure_repr = repr(closure_values)
    assert connection in closure_values
    assert 7 in closure_values
    assert IDENTITY not in closure_repr
    assert SOURCE not in closure_repr
    assert IDENTITY not in repr(runtime.spec)
    assert SOURCE not in repr(runtime.spec)


@pytest.mark.asyncio
async def test_async_setup_registers_inert_command_exactly_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registrations: list[object] = []
    monkeypatch.setattr(
        integration.websocket_api,
        "async_register_command",
        lambda hass_arg, handler: registrations.append((hass_arg, handler)),
    )

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup(hass, {}) is True

    assert len(registrations) == 1
    assert registrations[0][1] is websocket_subscribe_observations
    state = hass.data[DOMAIN]
    assert isinstance(state, DomainState)
    assert state.command_registered is True
    assert state.runtime is None


@pytest.mark.asyncio
async def test_incompatible_setup_creates_fixed_nonpersistent_repair_then_recovers(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await integration.async_setup(hass, {})
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    def incompatible(hass_arg: HomeAssistant) -> None:
        del hass_arg
        raise IncompatibleBluetoothApiError("private capability sentinel")

    monkeypatch.setattr(integration, "capability_check", incompatible)
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID)
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID
    assert issue.translation_placeholders == {
        "missing_capability": "Home Assistant 2026.7 public Bluetooth scanner cache API"
    }
    assert "private capability sentinel" not in repr(issue)

    created = ClosableRuntime()
    captured: dict[str, object] = {}

    def runtime_factory(hass_arg: HomeAssistant, **kwargs: object) -> object:
        captured["hass"] = hass_arg
        captured.update(kwargs)
        return created

    monkeypatch.setattr(integration, "capability_check", lambda hass_arg: None)
    monkeypatch.setattr(integration, "BleObservationRuntime", runtime_factory)
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True

    assert get_loaded_runtime(hass) is created
    assert callable(captured["create_background_task"])
    assert ir.async_get(hass).async_get_issue(DOMAIN, INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_setup_injects_config_entry_owned_background_task_factory(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await integration.async_setup(hass, {})
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    calls: list[tuple[HomeAssistant, str]] = []

    def create_entry_task(
        hass_arg: HomeAssistant,
        target: Coroutine[Any, Any, None],
        name: str,
        eager_start: bool = True,
    ) -> asyncio.Task[None]:
        del eager_start
        calls.append((hass_arg, name))
        return hass.async_create_background_task(target, name)

    monkeypatch.setattr(entry, "async_create_background_task", create_entry_task)
    captured: dict[str, object] = {}

    def runtime_factory(hass_arg: HomeAssistant, **kwargs: object) -> object:
        del hass_arg
        captured.update(kwargs)
        return ClosableRuntime()

    monkeypatch.setattr(integration, "capability_check", lambda hass_arg: None)
    monkeypatch.setattr(integration, "BleObservationRuntime", runtime_factory)
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True

    factory = cast(
        Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]],
        captured["create_background_task"],
    )

    async def noop() -> None:
        return None

    await factory(noop(), "entry-owned-test")
    assert calls == [(hass, "entry-owned-test")]


@pytest.mark.asyncio
async def test_unload_detaches_before_awaiting_close_and_is_repeatable(
    hass: HomeAssistant,
) -> None:
    runtime = ClosableRuntime(block_close=True)
    _install_runtime(hass, runtime)
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    unload = asyncio.create_task(integration.async_unload_entry(hass, cast(ConfigEntry, entry)))

    await runtime.close_started.wait()
    assert get_loaded_runtime(hass) is None
    runtime.close_release.set()
    assert await unload is True
    assert await integration.async_unload_entry(hass, cast(ConfigEntry, entry)) is True
    assert runtime.close_calls == 1


@pytest.mark.asyncio
async def test_remove_entry_deletes_compatibility_issue(
    hass: HomeAssistant,
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID,
    )
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)

    await integration.async_remove_entry(hass, cast(ConfigEntry, entry))

    assert ir.async_get(hass).async_get_issue(DOMAIN, INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_setup_entry_reload_unload_incompatible_and_recovery_matrix(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    created: list[ClosableRuntime] = []

    def runtime_factory(hass_arg: HomeAssistant, **kwargs: object) -> object:
        del hass_arg, kwargs
        runtime = ClosableRuntime()
        created.append(runtime)
        return runtime

    monkeypatch.setattr(integration, "capability_check", lambda hass_arg: None)
    monkeypatch.setattr(integration, "BleObservationRuntime", runtime_factory)

    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True
    assert get_loaded_runtime(hass) is created[0]
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True
    assert created[0].close_calls == 1
    assert get_loaded_runtime(hass) is created[1]

    assert await integration.async_unload_entry(hass, cast(ConfigEntry, entry)) is True
    assert created[1].close_calls == 1
    assert get_loaded_runtime(hass) is None

    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True
    assert get_loaded_runtime(hass) is created[2]

    def incompatible(hass_arg: HomeAssistant) -> None:
        del hass_arg
        raise IncompatibleBluetoothApiError("private transition sentinel")

    monkeypatch.setattr(integration, "capability_check", incompatible)
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is False
    assert created[2].close_calls == 1
    assert get_loaded_runtime(hass) is None

    monkeypatch.setattr(integration, "capability_check", lambda hass_arg: None)
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True
    assert get_loaded_runtime(hass) is created[3]
    assert ir.async_get(hass).async_get_issue(DOMAIN, INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID) is None


@pytest.mark.asyncio
async def test_subscribe_racing_unload_cancels_late_handle_and_returns_unavailable(
    hass: HomeAssistant,
) -> None:
    runtime = FakeRuntime(block_preparation=True)
    _install_runtime(hass, runtime)
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    connection = FakeConnection()
    subscribe = asyncio.create_task(
        _async_subscribe_observations(
            hass,
            cast(Any, connection),
            cast(Any, _message()),
        )
    )

    await runtime.started.wait()
    assert await integration.async_unload_entry(hass, cast(ConfigEntry, entry)) is True
    assert get_loaded_runtime(hass) is None
    assert runtime.close_calls == 1
    runtime.release.set()
    await subscribe

    assert runtime.subscription.cancel_calls == 1
    assert connection.subscriptions == {}
    assert connection.calls == [
        (
            "error",
            7,
            {
                "code": websocket_api.ERR_NOT_SUPPORTED,
                "message": "BLE observation runtime is unavailable.",
            },
        )
    ]


@pytest.mark.asyncio
async def test_old_cleanup_and_callback_cannot_affect_replacement_runtime(
    hass: HomeAssistant,
) -> None:
    old_runtime = FakeRuntime()
    state = _install_runtime(hass, old_runtime)
    connection = FakeConnection()
    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message()),
    )
    old_cleanup = connection.subscriptions.pop(7)

    replacement = FakeRuntime()
    state.runtime = cast(Any, replacement)
    await _async_subscribe_observations(
        hass,
        cast(Any, connection),
        cast(Any, _message(id=8)),
    )
    old_cleanup()
    old_cleanup()
    assert old_runtime.callback is not None
    old_runtime.callback(cast(Any, _event()))

    assert old_runtime.subscription.cancel_calls == 1
    assert replacement.subscription.cancel_calls == 0
    assert 8 in connection.subscriptions
    assert connection.calls == [("result", 7, None), ("result", 8, None)]


@pytest.mark.asyncio
async def test_real_admin_websocket_orders_and_minimizes_production_messages(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hass.config.components.remove("websocket_api")
    await integration.async_setup(hass, {})
    runtime = FakeRuntime(FakeSubscription(initial_event=_event()))
    _install_runtime(hass, runtime)
    websocket = await hass_ws_client()
    trackers = [
        {
            "trackerId": TRACKER_ID,
            "kind": "static-mac",
            "identity": IDENTITY,
        },
        {
            "trackerId": IBEACON_TRACKER_ID,
            "kind": "ibeacon",
            "identity": IBEACON_IDENTITY,
        },
    ]

    await websocket.send_json(_message(trackers=trackers))
    result = await websocket.receive_json()
    event = await websocket.receive_json()

    await websocket.send_json({"id": 8, "type": "unsubscribe_events", "subscription": 7})
    unsubscribed = await websocket.receive_json()

    control_sentinel = "private-control\nsource"
    await websocket.send_json(_message(id=9, scannerSources=[control_sentinel]))
    invalid = await websocket.receive_json()

    _install_runtime(
        hass,
        FakeRuntime(prepare_error=RuntimeError("private preparation sentinel")),
    )
    await websocket.send_json(_message(id=10))
    unavailable = await websocket.receive_json()

    assert result == {
        "id": 7,
        "type": "result",
        "success": True,
        "result": None,
    }
    assert event == {"id": 7, "type": "event", "event": _event()}
    assert unsubscribed == {
        "id": 8,
        "type": "result",
        "success": True,
        "result": None,
    }
    assert runtime.subscription.cancel_calls == 1
    assert invalid == {
        "id": 9,
        "type": "result",
        "success": False,
        "error": {
            "code": websocket_api.ERR_INVALID_FORMAT,
            "message": "Invalid BLE observation subscription.",
        },
    }
    assert unavailable == {
        "id": 10,
        "type": "result",
        "success": False,
        "error": {
            "code": websocket_api.ERR_NOT_SUPPORTED,
            "message": "BLE observation runtime is unavailable.",
        },
    }
    encoded = json.dumps([result, event, unsubscribed, invalid, unavailable])
    integration_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components.joyful_ble_positioning")
    )
    assert encoded.count(SOURCE) == 1
    for private_value in (
        IDENTITY,
        IBEACON_IDENTITY,
        control_sentinel,
        "private preparation sentinel",
    ):
        assert private_value not in encoded
        assert private_value not in integration_logs

    await websocket.close()


@pytest.mark.asyncio
async def test_real_non_admin_websocket_rejects_before_sensitive_parser(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass.config.components.remove("websocket_api")
    await integration.async_setup(hass, {})
    _install_runtime(hass, FakeRuntime())
    websocket = await hass_ws_client(hass, hass_read_only_access_token)
    parsed = False

    def forbidden_parse(message: object) -> object:
        nonlocal parsed
        parsed = True
        raise AssertionError(message)

    monkeypatch.setattr(
        "custom_components.joyful_ble_positioning.websocket.parse_subscription_spec",
        forbidden_parse,
    )
    await websocket.send_json(_message(trackers={"nested": IDENTITY}))
    response = await websocket.receive_json()

    assert parsed is False
    assert response == {
        "id": 7,
        "type": "result",
        "success": False,
        "error": {
            "code": websocket_api.ERR_UNAUTHORIZED,
            "message": "Unauthorized",
        },
    }
    assert IDENTITY not in json.dumps(response)

    await websocket.close()


def test_real_home_assistant_json_encoder_preserves_exact_minimized_shapes() -> None:
    event_bytes = messages.message_to_json_bytes(messages.event_message(7, _event()))
    error_bytes = messages.message_to_json_bytes(
        messages.error_message(
            8,
            websocket_api.ERR_INVALID_FORMAT,
            "Invalid BLE observation subscription.",
        )
    )

    encoded_event = json.loads(event_bytes)
    assert set(encoded_event) == {"id", "type", "event"}
    assert set(encoded_event["event"]) == set(_event())
    assert encoded_event["event"] == _event()
    encoded_error = json.loads(error_bytes)
    assert encoded_error == {
        "id": 8,
        "type": "result",
        "success": False,
        "error": {
            "code": websocket_api.ERR_INVALID_FORMAT,
            "message": "Invalid BLE observation subscription.",
        },
    }
    combined = event_bytes + error_bytes
    assert IDENTITY.encode() not in combined


def test_issue_translations_are_exact_and_placeholder_bounded() -> None:
    component = Path(__file__).parents[1] / "custom_components" / DOMAIN
    strings = json.loads((component / "strings.json").read_text())
    english = json.loads((component / "translations" / "en.json").read_text())

    assert strings["issues"] == english["issues"]
    issue = strings["issues"][INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID]
    assert set(issue) == {"title", "description"}
    assert issue["description"].count("{missing_capability}") == 1
    assert IDENTITY not in json.dumps(issue)
