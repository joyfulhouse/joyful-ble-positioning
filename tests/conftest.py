"""Shared fixtures for Joyful BLE Positioning tests."""

from __future__ import annotations

import importlib

import pytest
from homeassistant.core import HomeAssistant

pytest_plugins = "pytest_homeassistant_custom_component"

# Cache the repository package before the HA test fixture adds its bundled
# testing_config directory to the import path.
importlib.import_module("custom_components.joyful_ble_positioning")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
    hass: HomeAssistant,
) -> None:
    """Enable loading custom integrations in every test."""
    hass.config.components.update({"bluetooth", "websocket_api"})
