"""Binary sensor platform for Paperless KIplus runner."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_STATUS_UPDATED
from .runner import PaperlessRunner


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor from config entry."""

    runner: PaperlessRunner = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PaperlessRunnerRunningBinarySensor(entry.entry_id, runner)], True)


class PaperlessRunnerRunningBinarySensor(BinarySensorEntity):
    """Represent whether the runner is currently active."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_running"
        self._attr_name = "Paperless KIplus Läuft"

    async def async_added_to_hass(self) -> None:
        """Register dispatcher updates."""

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATUS_UPDATED,
                self.async_write_ha_state,
            )
        )

    @property
    def is_on(self) -> bool:
        """Return true if the runner is currently executing."""

        return self._runner.running
