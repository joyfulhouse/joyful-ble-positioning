"""Config-flow tests for Joyful BLE Positioning."""

from __future__ import annotations

from pathlib import Path

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

DOMAIN = "joyful_ble_positioning"
CONFIG_FLOW_PATH = (
    Path(__file__).parents[1] / "custom_components" / "joyful_ble_positioning" / "config_flow.py"
)


def _require_config_flow_scaffold() -> None:
    """Fail explicitly while the config-flow implementation is absent."""
    assert CONFIG_FLOW_PATH.is_file(), "the config flow has not been scaffolded"


async def test_user_step_shows_an_empty_form(hass: HomeAssistant) -> None:
    """The user flow starts with a confirmation-only empty form."""
    _require_config_flow_scaffold()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] is None
    assert result["data_schema"].schema == {}


async def test_user_step_creates_the_singleton_entry(hass: HomeAssistant) -> None:
    """Submitting the empty form creates the named singleton entry."""
    _require_config_flow_scaffold()
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Joyful BLE Positioning"
    assert result["data"] == {}
    assert result["options"] == {}
    assert result["result"].unique_id == DOMAIN


async def test_user_step_aborts_when_already_configured(hass: HomeAssistant) -> None:
    """A second user flow aborts instead of creating another entry."""
    _require_config_flow_scaffold()
    MockConfigEntry(domain=DOMAIN, data={}, unique_id=DOMAIN).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
