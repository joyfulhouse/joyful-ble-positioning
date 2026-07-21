"""Admin-only WebSocket transport for minimized BLE observations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, WS_TYPE
from .model import SubscriptionValidationError, parse_subscription_spec
from .runtime import (
    BleObservationRuntime,
    IncompatibleBluetoothApiError,
    RuntimeUnavailableError,
    ScannerSourceUnavailableError,
    TooManySubscriptionsError,
    WireEvent,
)

_LOGGER = logging.getLogger(__name__)

_INVALID_SUBSCRIPTION = "Invalid BLE observation subscription."
_RUNTIME_UNAVAILABLE = "BLE observation runtime is unavailable."
_SCANNER_NOT_FOUND = "BLE scanner source was not found."
_SUBSCRIPTION_LIMIT = "BLE observation subscription limit reached."

_NEUTRAL_SUBSCRIPTION_SCHEMA = cast(
    dict[str | vol.Marker, Any],
    {
        vol.Required("type"): WS_TYPE,
        vol.Extra: object,
    },
)


@dataclass(slots=True)
class DomainState:
    """Process-local integration state retained across entry reloads."""

    command_registered: bool = False
    runtime: BleObservationRuntime | None = None
    transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class DeferredSubscriptionCleanup:
    """Idempotent cleanup that is safe before runtime preparation completes."""

    __slots__ = ("_called", "_cancel", "_on_called")

    def __init__(self, on_called: Callable[[], None]) -> None:
        self._called = False
        self._cancel: Callable[[], None] | None = None
        self._on_called = on_called

    @property
    def called(self) -> bool:
        """Return whether cleanup was requested by the connection."""
        return self._called

    def attach(self, cancel: Callable[[], None]) -> None:
        """Attach the late runtime handle, cancelling it if already disconnected."""
        if self._cancel is not None:
            raise RuntimeError("cleanup already attached")
        if self._called:
            self._safe_cancel(cancel)
            return
        self._cancel = cancel

    def __call__(self) -> None:
        """Request cleanup exactly once."""
        if self._called:
            return
        self._called = True
        self._on_called()
        cancel = self._cancel
        self._cancel = None
        if cancel is not None:
            self._safe_cancel(cancel)

    @staticmethod
    def _safe_cancel(cancel: Callable[[], None]) -> None:
        try:
            cancel()
        except Exception as err:  # pragma: no cover - defensive integration boundary
            _LOGGER.warning("BLE subscription cleanup failed (%s)", type(err).__name__)


def get_loaded_runtime(hass: HomeAssistant) -> BleObservationRuntime | None:
    """Return only the currently attached runtime."""
    state = hass.data.get(DOMAIN)
    if not isinstance(state, DomainState):
        return None
    return state.runtime


def _retire_cleanup(
    connection: ActiveConnection,
    message_id: int,
    cleanup: DeferredSubscriptionCleanup,
) -> bool:
    """Remove and invoke our cleanup, returning whether the client is still live."""
    is_live = connection.subscriptions.get(message_id) is cleanup
    if is_live:
        connection.subscriptions.pop(message_id, None)
    cleanup.__call__()
    return is_live


def _send_preparation_error(
    connection: ActiveConnection,
    message_id: int,
    cleanup: DeferredSubscriptionCleanup,
    code: str,
    message: str,
) -> None:
    if _retire_cleanup(connection, message_id, cleanup):
        connection.send_error(message_id, code, message)


async def _async_subscribe_observations(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Validate and prepare one bounded observation stream for an admin."""
    message_id = cast(int, msg["id"])
    try:
        spec = parse_subscription_spec(msg)
    except SubscriptionValidationError:
        del msg
        connection.send_error(
            message_id,
            websocket_api.ERR_INVALID_FORMAT,
            _INVALID_SUBSCRIPTION,
        )
        return
    del msg

    runtime = get_loaded_runtime(hass)
    if runtime is None:
        del spec
        connection.send_error(
            message_id,
            websocket_api.ERR_NOT_SUPPORTED,
            _RUNTIME_UNAVAILABLE,
        )
        return

    acknowledged = False

    def deactivate_delivery() -> None:
        nonlocal acknowledged
        acknowledged = False

    cleanup = DeferredSubscriptionCleanup(deactivate_delivery)
    connection.subscriptions[message_id] = cleanup

    @callback
    def on_event(event: WireEvent) -> None:
        if acknowledged:
            connection.send_event(message_id, event)

    try:
        try:
            subscription = await runtime.async_subscribe(spec, on_event)
        finally:
            del spec
    except asyncio.CancelledError:
        _retire_cleanup(connection, message_id, cleanup)
        raise
    except ScannerSourceUnavailableError:
        _send_preparation_error(
            connection,
            message_id,
            cleanup,
            websocket_api.ERR_NOT_FOUND,
            _SCANNER_NOT_FOUND,
        )
        return
    except TooManySubscriptionsError:
        _send_preparation_error(
            connection,
            message_id,
            cleanup,
            websocket_api.ERR_NOT_ALLOWED,
            _SUBSCRIPTION_LIMIT,
        )
        return
    except IncompatibleBluetoothApiError, RuntimeUnavailableError:
        _send_preparation_error(
            connection,
            message_id,
            cleanup,
            websocket_api.ERR_NOT_SUPPORTED,
            _RUNTIME_UNAVAILABLE,
        )
        return
    except Exception as err:
        _LOGGER.warning("BLE subscription preparation failed (%s)", type(err).__name__)
        _send_preparation_error(
            connection,
            message_id,
            cleanup,
            websocket_api.ERR_NOT_SUPPORTED,
            _RUNTIME_UNAVAILABLE,
        )
        return

    cleanup.attach(subscription.cancel)
    if cleanup.called:
        return
    if get_loaded_runtime(hass) is not runtime:
        _send_preparation_error(
            connection,
            message_id,
            cleanup,
            websocket_api.ERR_NOT_SUPPORTED,
            _RUNTIME_UNAVAILABLE,
        )
        return

    try:
        connection.send_result(message_id)
    except Exception as err:  # pragma: no cover - connection implementation boundary
        _LOGGER.warning("BLE subscription acknowledgement failed (%s)", type(err).__name__)
        _retire_cleanup(connection, message_id, cleanup)
        return

    acknowledged = True
    try:
        await subscription.async_emit_initial()
    except asyncio.CancelledError:
        _retire_cleanup(connection, message_id, cleanup)
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
        return
    except Exception as err:
        _LOGGER.warning("BLE initial observation delivery failed (%s)", type(err).__name__)
        _retire_cleanup(connection, message_id, cleanup)


@websocket_api.websocket_command(_NEUTRAL_SUBSCRIPTION_SCHEMA)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_subscribe_observations(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Schedule one admin-authorized observation subscription."""
    await _async_subscribe_observations(hass, connection, msg)
