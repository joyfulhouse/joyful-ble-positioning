"""Joyful BLE Positioning integration lifecycle."""

from __future__ import annotations

from functools import partial
from typing import Any, cast

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID,
    REQUIRED_BLUETOOTH_CAPABILITY,
)
from .runtime import (
    BleObservationRuntime,
    CreateBackgroundTask,
    IncompatibleBluetoothApiError,
    capability_check,
)
from .websocket import DomainState, websocket_subscribe_observations


def _state(hass: HomeAssistant) -> DomainState:
    state = hass.data.get(DOMAIN)
    if not isinstance(state, DomainState):
        state = DomainState()
        hass.data[DOMAIN] = state
    return state


def _create_compatibility_issue(hass: HomeAssistant) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID,
        translation_placeholders={
            "missing_capability": REQUIRED_BLUETOOTH_CAPABILITY,
        },
    )


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the inert admin command once, before any entry is loaded."""
    del config
    state = _state(hass)
    if not state.command_registered:
        websocket_api.async_register_command(hass, websocket_subscribe_observations)
        state.command_registered = True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Attach a compatible runtime owned by this config entry."""
    await async_setup(hass, {})
    state = _state(hass)
    async with state.transition_lock:
        try:
            capability_check(hass)
        except IncompatibleBluetoothApiError:
            previous = state.runtime
            state.runtime = None
            _create_compatibility_issue(hass)
            if previous is not None:
                await previous.async_close()
            return False

        create_background_task = cast(
            CreateBackgroundTask,
            partial(entry.async_create_background_task, hass),
        )
        runtime = BleObservationRuntime(
            hass,
            create_background_task=create_background_task,
        )
        previous = state.runtime
        state.runtime = None
        if previous is not None:
            await previous.async_close()
        state.runtime = runtime
        ir.async_delete_issue(hass, DOMAIN, INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID)
        return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Detach the runtime before awaiting destruction of identity state."""
    del entry
    state = hass.data.get(DOMAIN)
    if not isinstance(state, DomainState):
        return True
    async with state.transition_lock:
        runtime = state.runtime
        state.runtime = None
        if runtime is not None:
            await runtime.async_close()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the compatibility repair when the singleton entry is deleted."""
    del entry
    ir.async_delete_issue(hass, DOMAIN, INCOMPATIBLE_BLUETOOTH_API_ISSUE_ID)
