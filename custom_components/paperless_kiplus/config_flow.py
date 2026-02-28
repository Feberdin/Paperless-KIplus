"""Config flow for Paperless KIplus Runner."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    TextSelectorConfig,
    TextSelectorType,
    TextSelector,
)

from .const import (
    CONF_ALL_DOCUMENTS,
    CONF_COMMAND,
    CONF_CONFIG_FILE,
    CONF_COOLDOWN_SECONDS,
    CONF_DRY_RUN,
    CONF_MAX_DOCUMENTS,
    CONF_MANAGED_CONFIG_ENABLED,
    CONF_MANAGED_CONFIG_YAML,
    CONF_METRICS_FILE,
    CONF_WORKDIR,
    DEFAULT_ALL_DOCUMENTS,
    DEFAULT_COMMAND,
    DEFAULT_CONFIG_FILE,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DRY_RUN,
    DEFAULT_MAX_DOCUMENTS,
    DEFAULT_MANAGED_CONFIG_ENABLED,
    DEFAULT_MANAGED_CONFIG_YAML,
    DEFAULT_METRICS_FILE,
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
                vol.Required(CONF_CONFIG_FILE, default=DEFAULT_CONFIG_FILE): TextSelector(),
                vol.Required(CONF_METRICS_FILE, default=DEFAULT_METRICS_FILE): TextSelector(),
                vol.Required(CONF_DRY_RUN, default=DEFAULT_DRY_RUN): BooleanSelector(),
                vol.Required(CONF_ALL_DOCUMENTS, default=DEFAULT_ALL_DOCUMENTS): BooleanSelector(),
                vol.Required(CONF_MAX_DOCUMENTS, default=DEFAULT_MAX_DOCUMENTS): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=5000,
                        step=1,
                        mode="box",
                    )
                ),
                vol.Required(
                    CONF_MANAGED_CONFIG_ENABLED,
                    default=DEFAULT_MANAGED_CONFIG_ENABLED,
                ): BooleanSelector(),
                vol.Required(
                    CONF_MANAGED_CONFIG_YAML,
                    default=DEFAULT_MANAGED_CONFIG_YAML,
                ): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        multiline=True,
                    )
                ),
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
                    CONF_METRICS_FILE,
                    default=options.get(
                        CONF_METRICS_FILE,
                        data.get(CONF_METRICS_FILE, DEFAULT_METRICS_FILE),
                    ),
                ): TextSelector(),
                vol.Required(
                    CONF_CONFIG_FILE,
                    default=options.get(
                        CONF_CONFIG_FILE,
                        data.get(CONF_CONFIG_FILE, DEFAULT_CONFIG_FILE),
                    ),
                ): TextSelector(),
                vol.Required(
                    CONF_DRY_RUN,
                    default=options.get(
                        CONF_DRY_RUN,
                        data.get(CONF_DRY_RUN, DEFAULT_DRY_RUN),
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_ALL_DOCUMENTS,
                    default=options.get(
                        CONF_ALL_DOCUMENTS,
                        data.get(CONF_ALL_DOCUMENTS, DEFAULT_ALL_DOCUMENTS),
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_MAX_DOCUMENTS,
                    default=options.get(
                        CONF_MAX_DOCUMENTS,
                        data.get(CONF_MAX_DOCUMENTS, DEFAULT_MAX_DOCUMENTS),
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=5000,
                        step=1,
                        mode="box",
                    )
                ),
                vol.Required(
                    CONF_MANAGED_CONFIG_ENABLED,
                    default=options.get(
                        CONF_MANAGED_CONFIG_ENABLED,
                        data.get(CONF_MANAGED_CONFIG_ENABLED, DEFAULT_MANAGED_CONFIG_ENABLED),
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_MANAGED_CONFIG_YAML,
                    default=options.get(
                        CONF_MANAGED_CONFIG_YAML,
                        data.get(CONF_MANAGED_CONFIG_YAML, DEFAULT_MANAGED_CONFIG_YAML),
                    ),
                ): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        multiline=True,
                    )
                ),
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
