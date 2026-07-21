"""Public packaging, CI, and side-effect boundary contracts."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from homeassistant.components.websocket_api import messages
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from PIL import Image
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import joyful_ble_positioning as integration
from custom_components.joyful_ble_positioning.const import (
    DOMAIN,
    INTEGRATION_VERSION,
)
from custom_components.joyful_ble_positioning.runtime import (
    ObservedEvent,
    StaleEvent,
    as_wire_event,
)

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / DOMAIN
MANIFEST = COMPONENT / "manifest.json"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_TAG = "v0.1.0"
PUBLIC_GIT_NAME = "Bryan Li"
PUBLIC_GIT_EMAIL = "15131870+btli@users.noreply.github.com"
UV_VERSION = "0.11.29"
UV_LINUX_X86_64_SHA256 = "04f8b82f5d47f0512dcd32c67a4a6f16a0ea27c81537c338fd0ad6b23cebe829"

CHECKOUT_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_UV_SHA = "11f9893b081a58869d3b5fccaea48c9e9e46f990"
HACS_ACTION_SHA = "1ebf01c408f29afcb6406bd431bc98fd8cbb15aa"
HASSFEST_ACTION_SHA = "e3fb68ebda13d88a0d695082f471ba2c83d025fb"

EXPECTED_COMPONENT_FILES = {
    "__init__.py",
    "brand/icon.png",
    "brand/icon@2x.png",
    "config_flow.py",
    "const.py",
    "manifest.json",
    "matcher.py",
    "model.py",
    "runtime.py",
    "strings.json",
    "translations/en.json",
    "websocket.py",
}

EXPECTED_RELEASE_FILES = {
    ".github/workflows/ci.yml",
    ".gitignore",
    "BRAND.md",
    "LICENSE",
    "README.md",
    "artwork/joyful-ble-positioning.svg",
    "custom_components/__init__.py",
    *(f"custom_components/{DOMAIN}/{path}" for path in EXPECTED_COMPONENT_FILES),
    "hacs.json",
    "pyproject.toml",
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/test_config_flow.py",
    "tests/test_contract.py",
    "tests/test_manifest.py",
    "tests/test_matcher.py",
    "tests/test_runtime.py",
    "tests/test_websocket.py",
    "uv.lock",
}

EXPECTED_ASSET_SHA256 = {
    "artwork/joyful-ble-positioning.svg": (
        "d29f98e3d5926c20ecc5f5899636b1f12b68109ebd68f1184f5859897166cf04"
    ),
    f"custom_components/{DOMAIN}/brand/icon.png": (
        "f373f42035732ad0617fe39fbecbca545c046d4e90670fffbcb1b435751fb8b5"
    ),
    f"custom_components/{DOMAIN}/brand/icon@2x.png": (
        "f1e42550deabd9dc09bb0cc9f0013a296b452a3ff9ae3998b3c95dab2151665c"
    ),
}

EXPECTED_ACTION_USES = [
    f"actions/checkout@{CHECKOUT_SHA}",
    f"astral-sh/setup-uv@{SETUP_UV_SHA}",
    f"hacs/action@{HACS_ACTION_SHA}",
    f"actions/checkout@{CHECKOUT_SHA}",
    f"home-assistant/actions/hassfest@{HASSFEST_ACTION_SHA}",
]


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _tracked_files() -> set[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "-z"],
        cwd=ROOT,
    )
    return {item.decode() for item in output.split(b"\0") if item}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def test_hacs_package_layout_and_metadata_are_exact() -> None:
    tracked = _tracked_files()
    component_prefix = f"custom_components/{DOMAIN}/"
    component_files = {
        path.removeprefix(component_prefix) for path in tracked if path.startswith(component_prefix)
    }
    custom_component_directories = {
        path.split("/", 2)[1]
        for path in tracked
        if path.startswith("custom_components/") and path.count("/") >= 2
    }

    assert tracked == EXPECTED_RELEASE_FILES
    assert custom_component_directories == {DOMAIN}
    assert component_files == EXPECTED_COMPONENT_FILES

    manifest = _json(MANIFEST)
    assert set(manifest) == {
        "codeowners",
        "config_flow",
        "dependencies",
        "documentation",
        "domain",
        "integration_type",
        "iot_class",
        "issue_tracker",
        "name",
        "requirements",
        "single_config_entry",
        "version",
    }
    assert manifest["codeowners"] == ["@btli"]
    assert manifest["requirements"] == []

    hacs = _json(ROOT / "hacs.json")
    assert hacs == {
        "name": "Joyful BLE Positioning",
        "homeassistant": "2026.7.2",
        "render_readme": True,
    }
    assert "filename" not in hacs
    assert hacs.get("zip_release", False) is False


def test_release_tree_contains_no_generated_or_sensitive_paths() -> None:
    tracked = _tracked_files()
    forbidden_names = {
        ".DS_Store",
        ".env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "credentials.json",
        "secrets.yaml",
    }
    forbidden_suffixes = {".key", ".pem", ".p12", ".pyc"}

    for tracked_path in tracked:
        path = Path(tracked_path)
        assert forbidden_names.isdisjoint(path.parts)
        assert path.suffix not in forbidden_suffixes
        assert not tracked_path.startswith("tests/fixtures/private/")

    if os.environ.get("RELEASE_CONTRACT") == "1":
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=ROOT,
            text=True,
        )
        assert status == ""


def test_public_history_and_head_use_safe_git_identities() -> None:
    history = subprocess.check_output(
        ["git", "log", "--all", "--format=%an%x09%ae%x09%cn%x09%ce"],
        cwd=ROOT,
        text=True,
    ).splitlines()
    assert history

    for record in history:
        author_name, author_email, committer_name, committer_email = record.split("\t")
        assert author_name and committer_name
        for email in (author_email, committer_email):
            assert "@" in email
            assert not email.casefold().endswith(".local")
            assert "@localhost" not in email.casefold()

    head_identity = subprocess.check_output(
        ["git", "show", "-s", "--format=%an%x09%ae%x09%cn%x09%ce", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    assert head_identity == "\t".join(
        (PUBLIC_GIT_NAME, PUBLIC_GIT_EMAIL, PUBLIC_GIT_NAME, PUBLIC_GIT_EMAIL)
    )


def test_version_is_identical_across_package_and_public_installation_text() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = _json(MANIFEST)
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert pyproject["project"]["version"] == "0.1.0"
    assert manifest["version"] == "0.1.0"
    assert INTEGRATION_VERSION == "0.1.0"
    assert RELEASE_TAG in readme
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        assert os.environ["GITHUB_REF_NAME"] == RELEASE_TAG


def test_original_brand_assets_are_decodable_scaled_pngs() -> None:
    icon = COMPONENT / "brand" / "icon.png"
    icon_2x = COMPONENT / "brand" / "icon@2x.png"

    for path, size in ((icon, (256, 256)), (icon_2x, (512, 512))):
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
            assert image.format == "PNG"
            assert image.mode == "RGBA"
            assert image.size == size
            assert image.info == {}
            assert len(image.getexif()) == 0

    assert 1_000 <= icon.stat().st_size <= 250_000
    assert 2_000 <= icon_2x.stat().st_size <= 750_000
    for path, expected_hash in EXPECTED_ASSET_SHA256.items():
        assert _sha256(ROOT / path) == expected_hash

    provenance = (ROOT / "BRAND.md").read_text(encoding="utf-8")
    assert "original" in provenance.lower()
    assert "MIT" in provenance
    assert "256" in provenance and "512" in provenance


def test_ci_is_least_privilege_pinned_and_complete() -> None:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    workflow = cast(
        dict[str, Any],
        yaml.load(workflow_text, Loader=yaml.BaseLoader),
    )

    assert "pull_request_target" not in workflow_text
    assert set(workflow["on"]) == {
        "push",
        "pull_request",
        "schedule",
        "workflow_dispatch",
    }
    assert workflow["on"] == {
        "push": {"branches": ["main"], "tags": ["v*"]},
        "pull_request": "",
        "schedule": [{"cron": "17 9 * * *"}],
        "workflow_dispatch": "",
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"python", "hacs", "hassfest"}

    python_job = workflow["jobs"]["python"]
    assert python_job["permissions"] == {"contents": "read"}
    assert python_job["runs-on"] == "ubuntu-latest"
    assert python_job["env"] == {"RELEASE_CONTRACT": "1"}
    python_steps = python_job["steps"]
    assert python_steps[0] == {
        "uses": f"actions/checkout@{CHECKOUT_SHA}",
        "with": {"fetch-depth": "0", "persist-credentials": "false"},
    }
    assert python_steps[1]["uses"] == f"astral-sh/setup-uv@{SETUP_UV_SHA}"
    assert python_steps[1]["with"] == {
        "checksum": UV_LINUX_X86_64_SHA256,
        "enable-cache": "true",
        "python-version": "3.14",
        "version": UV_VERSION,
    }
    commands = [step["run"] for step in python_steps if "run" in step]
    for command in (
        "uv sync --locked",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy custom_components/joyful_ble_positioning",
        "uv run pytest -q",
    ):
        assert command in commands
    tag_step = next(
        step for step in python_steps if step.get("name") == "Verify tag matches manifest version"
    )
    assert tag_step["if"] == "startsWith(github.ref, 'refs/tags/')"
    assert tag_step["shell"] == "bash"
    assert "uv run python -c" in tag_step["run"]
    assert 'test "${GITHUB_REF_NAME#v}" = "${manifest_version}"' in tag_step["run"]

    hacs_job = workflow["jobs"]["hacs"]
    assert hacs_job["permissions"] == {}
    assert any(
        step.get("uses") == f"hacs/action@{HACS_ACTION_SHA}"
        and step.get("with") == {"category": "integration", "comment": "false"}
        for step in hacs_job["steps"]
    )
    hassfest_job = workflow["jobs"]["hassfest"]
    assert hassfest_job["permissions"] == {"contents": "read"}
    assert any(
        step.get("uses") == f"home-assistant/actions/hassfest@{HASSFEST_ACTION_SHA}"
        for step in hassfest_job["steps"]
    )

    uses = [
        step["uses"] for job in workflow["jobs"].values() for step in job["steps"] if "uses" in step
    ]
    assert uses == EXPECTED_ACTION_USES
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)
    assert "ghcr.io/hacs/action:main" in workflow_text
    assert "upstream-managed validator image" in workflow_text


def test_runtime_source_has_no_entity_service_event_recorder_or_network_path() -> None:
    forbidden_import_prefixes = (
        "aiohttp",
        "httpx",
        "requests",
        "urllib",
        "custom_components.bermuda",
        "homeassistant.components.history",
        "homeassistant.components.recorder",
        "homeassistant.components.statistics",
        "homeassistant.helpers.aiohttp_client",
    )
    forbidden_calls = {
        "hass.bus.async_fire",
        "hass.services.async_register",
        "device_registry.async_get_or_create",
        "entity_registry.async_get_or_create",
    }

    for path in COMPONENT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports: list[str] = []
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.append(node.module)
            elif isinstance(node, ast.Call):
                dotted = _dotted_name(node.func)
                if dotted is not None:
                    calls.add(dotted)

        assert not any(
            imported == prefix or imported.startswith(f"{prefix}.")
            for imported in imports
            for prefix in forbidden_import_prefixes
        )
        assert forbidden_calls.isdisjoint(calls)


@pytest.mark.asyncio
async def test_setup_and_unload_have_no_entity_device_service_or_event_side_effects(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError((args, kwargs))

    monkeypatch.setattr(type(hass.services), "async_register", forbidden)
    monkeypatch.setattr(type(hass.bus), "async_fire", forbidden)
    monkeypatch.setattr(dr, "async_get", forbidden)
    monkeypatch.setattr(er, "async_get", forbidden)
    monkeypatch.setattr(integration, "capability_check", lambda hass_arg: None)

    class Runtime:
        async def async_close(self) -> None:
            return None

    monkeypatch.setattr(integration, "BleObservationRuntime", lambda *args, **kwargs: Runtime())
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, cast(ConfigEntry, entry)) is True
    assert await integration.async_unload_entry(hass, cast(ConfigEntry, entry)) is True


def test_real_serializer_emits_only_exact_bounded_json_values() -> None:
    observed = as_wire_event(
        ObservedEvent(
            stream_id="00000000-0000-4000-8000-000000000099",
            state="observed",
            tracker_id="00000000-0000-4000-8000-000000000001",
            scanner_source="synthetic-scanner",
            sequence=1,
            rssi_dbm=-61,
            tx_power_dbm=-8,
            age_ms=125,
            received_at="2026-01-01T00:00:00.000Z",
        )
    )
    stale = as_wire_event(
        StaleEvent(
            stream_id="00000000-0000-4000-8000-000000000099",
            state="stale",
            tracker_id="00000000-0000-4000-8000-000000000001",
            scanner_source="synthetic-scanner",
            sequence=2,
            age_ms=10_000,
        )
    )

    observed_message = json.loads(
        messages.message_to_json_bytes(messages.event_message(7, observed))
    )
    stale_message = json.loads(messages.message_to_json_bytes(messages.event_message(7, stale)))
    assert set(observed_message) == {"id", "type", "event"}
    assert set(observed_message["event"]) == {
        "streamId",
        "state",
        "trackerId",
        "scannerSource",
        "sequence",
        "rssiDbm",
        "txPowerDbm",
        "ageMs",
        "receivedAt",
    }
    assert set(stale_message["event"]) == {
        "streamId",
        "state",
        "trackerId",
        "scannerSource",
        "sequence",
        "ageMs",
    }
    event = observed_message["event"]
    assert type(event["sequence"]) is int and event["sequence"] >= 1
    assert type(event["rssiDbm"]) is int and -126 <= event["rssiDbm"] <= -1
    assert type(event["txPowerDbm"]) is int and -126 <= event["txPowerDbm"] <= 20
    assert type(event["ageMs"]) is int and 0 <= event["ageMs"] <= 86_400_000
    serialized = json.dumps([observed_message, stale_message])
    for forbidden in (
        "address",
        "identity",
        "localName",
        "manufacturerData",
        "serviceData",
        "serviceUuids",
        "platformData",
    ):
        assert forbidden not in serialized
