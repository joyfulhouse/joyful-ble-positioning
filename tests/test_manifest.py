"""Manifest contract tests for Joyful BLE Positioning."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from homeassistant.const import Platform

from custom_components import joyful_ble_positioning as integration

ROOT = Path(__file__).parents[1]
COMPONENT_DIR = ROOT / "custom_components" / "joyful_ble_positioning"
MANIFEST_PATH = COMPONENT_DIR / "manifest.json"


def _load_manifest() -> dict[str, Any]:
    """Load the integration manifest after proving the scaffold exists."""
    assert MANIFEST_PATH.is_file(), "the integration manifest has not been scaffolded"
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_manifest_declares_exact_integration_contract() -> None:
    """The manifest declares the versioned singleton service integration."""
    manifest = _load_manifest()

    assert manifest["domain"] == "joyful_ble_positioning"
    assert manifest["name"] == "Joyful BLE Positioning"
    assert manifest["version"] == "0.1.0"
    assert manifest["issue_tracker"] == (
        "https://github.com/joyfulhouse/joyful-ble-positioning/issues"
    )
    assert manifest["dependencies"] == ["bluetooth", "websocket_api"]
    assert manifest["single_config_entry"] is True
    assert manifest["integration_type"] == "service"
    assert manifest["iot_class"] == "calculated"
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == []


def test_manifest_key_order_matches_hassfest_contract() -> None:
    """Hassfest requires domain/name first and every remaining key sorted."""
    keys = list(_load_manifest())

    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_async_setup_is_declared_config_entry_only() -> None:
    """The inert async_setup hook must explicitly reject YAML configuration."""
    schema = integration.CONFIG_SCHEMA

    assert schema.__module__ == "homeassistant.helpers.config_validation"
    assert schema.__qualname__.startswith("_no_yaml_config_schema.<locals>.")
    assert inspect.getclosurevars(schema).nonlocals["domain"] == "joyful_ble_positioning"


def test_integration_has_no_home_assistant_platforms() -> None:
    """The bridge remains entity-free by exposing no platform modules."""
    _load_manifest()

    platform_files = {f"{platform.value}.py" for platform in Platform}
    component_files = {path.name for path in COMPONENT_DIR.glob("*.py")}
    assert component_files.isdisjoint(platform_files)


def test_component_source_contains_no_forbidden_private_or_stateful_contracts() -> None:
    """Every runtime module stays on the narrow public, current-state API boundary."""
    forbidden = {
        "_get_manager",
        "subscribe_advertisements",
        ".storage",
        "custom_components.bermuda",
        "async_get_clientsession",
        "discovered_devices_and_advertisement_data_history",
    }
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(COMPONENT_DIR.rglob("*.py"))
    )

    assert all(fragment not in source for fragment in forbidden)
