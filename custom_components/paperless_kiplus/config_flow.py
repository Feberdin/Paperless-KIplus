"""Config flow for Paperless KIplus Runner."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
import yaml

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_ALL_DOCUMENTS,
    CONF_COOLDOWN_SECONDS,
    CONF_DRY_RUN,
    CONF_INPUT_COST_PER_1K_TOKENS_EUR,
    CONF_MANAGED_CONFIG_YAML,
    CONF_MAX_DOCUMENTS,
    CONF_OUTPUT_COST_PER_1K_TOKENS_EUR,
    DEFAULT_ALL_DOCUMENTS,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DRY_RUN,
    DEFAULT_INPUT_COST_PER_1K_TOKENS_EUR,
    DEFAULT_MANAGED_CONFIG_YAML,
    DEFAULT_MAX_DOCUMENTS,
    DEFAULT_OUTPUT_COST_PER_1K_TOKENS_EUR,
    DOMAIN,
)


def _description_placeholders() -> dict[str, str]:
    """Hilfetexte für die Form-Ansicht."""

    return {
        "dry_run_help": (
            "Dry-Run: Es werden keine Änderungen in Paperless gespeichert. "
            "Die KI analysiert Dokumente und zeigt nur Vorschläge an "
            "(Dokumenttyp, Korrespondent, Speicherpfad, Tags, Datum, Notiz). "
            "Ideal zum sicheren Testen."
        ),
        "all_documents_help": (
            "Alle Dokumente: Verarbeitet einmalig den gesamten Bestand (bis Max. Dokumente). "
            "Wenn AUS, wird nur der in deiner YAML definierte Filter verarbeitet "
            "(z. B. process_only_tag wie #NEU)."
        ),
        "pricing_source": (
            "Standardwerte: OpenAI GPT-4.1 mini (Input 0.40 USD/1M, Output 1.60 USD/1M, "
            "entspricht 0.0004/0.0016 pro 1.000 Tokens). "
            "Quelle: https://platform.openai.com/docs/pricing"
        ),
        "yaml_help": (
            "Bitte den kompletten YAML-Text immer hier einfügen. "
            "Kein externes YAML nutzen. "
            "Hilfe/Prompt: https://github.com/Feberdin/Paperless-KIplus?tab=readme-ov-file#-chatgpt-prompt-f%C3%BCr-eigene-yaml-konfig"
        ),
    }


class PaperlessKIplusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Paperless KIplus Runner."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_managed_yaml_input(user_input)
            if not errors:
                return self.async_create_entry(title="Paperless KIplus Runner", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_DRY_RUN, default=DEFAULT_DRY_RUN): BooleanSelector(),
                vol.Required(CONF_ALL_DOCUMENTS, default=DEFAULT_ALL_DOCUMENTS): BooleanSelector(),
                vol.Required(
                    CONF_INPUT_COST_PER_1K_TOKENS_EUR,
                    default=str(DEFAULT_INPUT_COST_PER_1K_TOKENS_EUR),
                ): TextSelector(),
                vol.Required(
                    CONF_OUTPUT_COST_PER_1K_TOKENS_EUR,
                    default=str(DEFAULT_OUTPUT_COST_PER_1K_TOKENS_EUR),
                ): TextSelector(),
                vol.Required(CONF_MAX_DOCUMENTS, default=DEFAULT_MAX_DOCUMENTS): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=5000,
                        step=1,
                        mode="box",
                    )
                ),
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
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=_description_placeholders(),
        )

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

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_managed_yaml_input(user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)

        options = self._config_entry.options
        data = self._config_entry.data

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_DRY_RUN,
                    default=options.get(CONF_DRY_RUN, data.get(CONF_DRY_RUN, DEFAULT_DRY_RUN)),
                ): BooleanSelector(),
                vol.Required(
                    CONF_ALL_DOCUMENTS,
                    default=options.get(
                        CONF_ALL_DOCUMENTS,
                        data.get(CONF_ALL_DOCUMENTS, DEFAULT_ALL_DOCUMENTS),
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_INPUT_COST_PER_1K_TOKENS_EUR,
                    default=options.get(
                        CONF_INPUT_COST_PER_1K_TOKENS_EUR,
                        data.get(
                            CONF_INPUT_COST_PER_1K_TOKENS_EUR,
                            DEFAULT_INPUT_COST_PER_1K_TOKENS_EUR,
                        ),
                    ),
                ): TextSelector(),
                vol.Required(
                    CONF_OUTPUT_COST_PER_1K_TOKENS_EUR,
                    default=options.get(
                        CONF_OUTPUT_COST_PER_1K_TOKENS_EUR,
                        data.get(
                            CONF_OUTPUT_COST_PER_1K_TOKENS_EUR,
                            DEFAULT_OUTPUT_COST_PER_1K_TOKENS_EUR,
                        ),
                    ),
                ): TextSelector(),
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

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders=_description_placeholders(),
        )


def _validate_managed_yaml_input(user_input: dict[str, Any]) -> dict[str, str]:
    """Validate managed YAML input.

    Die Integration verwaltet YAML immer intern in Home Assistant,
    daher ist ein valider YAML-Text zwingend erforderlich.
    """

    raw_yaml = str(user_input.get(CONF_MANAGED_CONFIG_YAML, "")).strip()
    if not raw_yaml:
        return {"base": "managed_yaml_required"}

    try:
        parsed = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return {"base": "managed_yaml_invalid"}

    if not isinstance(parsed, dict):
        return {"base": "managed_yaml_invalid"}

    required = ("paperless_url", "paperless_token", "ai_api_key", "ai_model")
    missing = [key for key in required if not parsed.get(key)]
    if missing:
        return {"base": "managed_yaml_missing_required"}

    return {}
