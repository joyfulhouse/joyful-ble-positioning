"""Shared fixtures for Joyful BLE Positioning tests."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant

pytest_plugins = "pytest_homeassistant_custom_component"

# Home Assistant installs aiousbwatcher only on its Linux targets, while importing
# the public Bluetooth API traverses the USB integration on macOS test hosts.
if importlib.util.find_spec("aiousbwatcher") is None:
    aiousbwatcher = ModuleType("aiousbwatcher")
    aiousbwatcher.AIOUSBWatcher = type("AIOUSBWatcher", (), {})
    aiousbwatcher.InotifyNotAvailableError = type("InotifyNotAvailableError", (Exception,), {})
    sys.modules["aiousbwatcher"] = aiousbwatcher
if "homeassistant.components.usb" not in sys.modules:
    sys.modules["homeassistant.components.usb"] = ModuleType("homeassistant.components.usb")

# Cache the repository package before the HA test fixture adds its bundled
# testing_config directory to the import path.
importlib.import_module("custom_components.joyful_ble_positioning")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
    hass: HomeAssistant,
) -> None:
    """Enable loading custom integrations in every test."""
    for component in ("bluetooth", "websocket_api"):
        hass.config.components.add(component)
