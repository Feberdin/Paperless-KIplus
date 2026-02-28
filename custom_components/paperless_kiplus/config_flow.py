"""Config flow for Paperless KIplus Runner."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import NumberSelector, NumberSelectorConfig, TextSelector

from .const import (
    CONF_COMMAND,
    CONF_COOLDOWN_SECONDS,
    CONF_WORKDIR,
    DEFAULT_COMMAND,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_WORKDIR,
    DOMAIN,
)


class PaperlessKIplusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Paperless KIplus Runner."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""

        if user_input is not None:
            return self.async_create_entry(title="Paperless KIplus Runner", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_COMMAND, default=DEFAULT_COMMAND): TextSelector(),
                vol.Required(CONF_WORKDIR, default=DEFAULT_WORKDIR): TextSelector(),
                vol.Required(
                    CONF_COOLDOWN_SECONDS,
                    default=DEFAULT_COOLDOWN_SECONDS,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=86400,
                        step=10,
                        mode="box",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""

        return PaperlessKIplusOptionsFlow(config_entry)


class PaperlessKIplusOptionsFlow(config_entries.OptionsFlow):
    """Handle options for the integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Manage options."""

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        data = self._config_entry.data

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_COMMAND,
                    default=options.get(CONF_COMMAND, data.get(CONF_COMMAND, DEFAULT_COMMAND)),
                ): TextSelector(),
                vol.Required(
                    CONF_WORKDIR,
                    default=options.get(CONF_WORKDIR, data.get(CONF_WORKDIR, DEFAULT_WORKDIR)),
                ): TextSelector(),
                vol.Required(
                    CONF_COOLDOWN_SECONDS,
                    default=options.get(
                        CONF_COOLDOWN_SECONDS,
                        data.get(CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS),
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=86400,
                        step=10,
                        mode="box",
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
