"""Manifest contract tests for Joyful BLE Positioning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.const import Platform

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


def test_integration_has_no_home_assistant_platforms() -> None:
    """The bridge remains entity-free by exposing no platform modules."""
    _load_manifest()

    platform_files = {f"{platform.value}.py" for platform in Platform}
    component_files = {path.name for path in COMPONENT_DIR.glob("*.py")}
    assert component_files.isdisjoint(platform_files)
