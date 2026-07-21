"""Config flow for Joyful BLE Positioning."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult

from .const import DOMAIN


class JoyfulBlePositioningConfigFlow(
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    """Configure the singleton Joyful BLE Positioning integration."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the singleton entry after an explicit confirmation."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Joyful BLE Positioning", data={})
