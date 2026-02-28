"""Home Assistant integration for Paperless KIplus runner."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ENTRY_ID,
    ATTR_FORCE,
    ATTR_WAIT,
    CONF_COMMAND,
    CONF_COOLDOWN_SECONDS,
    CONF_WORKDIR,
    DEFAULT_COMMAND,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_WORKDIR,
    DOMAIN,
    SERVICE_RUN,
)
from .runner import PaperlessRunner

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor"]

RUN_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_FORCE, default=False): cv.boolean,
        vol.Optional(ATTR_WAIT, default=False): cv.boolean,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
    }
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up from YAML (unused, config flow only)."""

    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up integration from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    options = entry.options
    data = entry.data

    command = options.get(CONF_COMMAND, data.get(CONF_COMMAND, DEFAULT_COMMAND))
    workdir = options.get(CONF_WORKDIR, data.get(CONF_WORKDIR, DEFAULT_WORKDIR))
    cooldown_seconds = int(
        options.get(CONF_COOLDOWN_SECONDS, data.get(CONF_COOLDOWN_SECONDS, DEFAULT_COOLDOWN_SECONDS))
    )

    runner = PaperlessRunner(
        hass,
        command=command,
        workdir=workdir,
        cooldown_seconds=cooldown_seconds,
    )
    hass.data[DOMAIN][entry.entry_id] = runner

    if not hass.services.has_service(DOMAIN, SERVICE_RUN):

        async def _handle_run(call: ServiceCall) -> None:
            force = call.data.get(ATTR_FORCE, False)
            wait = call.data.get(ATTR_WAIT, False)
            target_entry_id = call.data.get(ATTR_ENTRY_ID)

            if target_entry_id:
                target_runners = [
                    (target_entry_id, hass.data[DOMAIN].get(target_entry_id))
                ]
            else:
                target_runners = list(hass.data[DOMAIN].items())

            tasks = []
            for entry_id, target_runner in target_runners:
                if target_runner is None:
                    _LOGGER.warning("Paperless KIplus entry '%s' not found", entry_id)
                    continue
                if wait:
                    await target_runner.async_run(force=force)
                else:
                    tasks.append(hass.async_create_task(target_runner.async_run(force=force)))

            if tasks:
                _LOGGER.info("Started %s Paperless KIplus background run task(s)", len(tasks))

        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN,
            _handle_run,
            schema=RUN_SERVICE_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)

    if not hass.data[DOMAIN] and hass.services.has_service(DOMAIN, SERVICE_RUN):
        hass.services.async_remove(DOMAIN, SERVICE_RUN)

    return True
