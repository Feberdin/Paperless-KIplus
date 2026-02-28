"""Sensor platform for Paperless KIplus runner."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorEntity
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
    """Set up sensor from config entry."""

    runner: PaperlessRunner = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PaperlessRunnerStatusSensor(entry.entry_id, runner)], True)


class PaperlessRunnerStatusSensor(SensorEntity):
    """Expose the latest runner status and metadata."""

    _attr_icon = "mdi:file-document-cog"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_status"
        self._attr_name = "Paperless KIplus Status"

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
    def native_value(self) -> str:
        """Return current status string."""

        return self._runner.last_status

    @property
    def extra_state_attributes(self) -> dict[str, str | int | None]:
        """Return useful status details for troubleshooting."""

        def _iso(ts: datetime | None) -> str | None:
            return ts.isoformat() if ts else None

        return {
            "message": self._runner.last_message,
            "running": self._runner.running,
            "last_exit_code": self._runner.last_exit_code,
            "last_started": _iso(self._runner.last_started),
            "last_finished": _iso(self._runner.last_finished),
            "cooldown_until": _iso(self._runner.cooldown_until),
            "command": self._runner.command,
            "workdir": self._runner.workdir,
            "stdout_tail": self._runner.last_stdout_tail,
            "stderr_tail": self._runner.last_stderr_tail,
        }
