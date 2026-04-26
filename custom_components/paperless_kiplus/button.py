"""Button platform for Paperless KIplus runner."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runner import PaperlessRunner


def _device_info(entry_id: str) -> DeviceInfo:
    """Gemeinsame Gerätezuordnung für Button-Entitäten."""

    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Paperless KIplus Runner",
        manufacturer="Feberdin",
        model="Paperless KIplus",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities from config entry."""

    runner: PaperlessRunner = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            PaperlessRunnerBackfillButton(entry.entry_id, runner),
            PaperlessRunnerStopButton(entry.entry_id, runner),
            PaperlessRunnerHardStopButton(entry.entry_id, runner),
            PaperlessRunnerResumeButton(entry.entry_id, runner),
            PaperlessRunnerResetMetricsButton(entry.entry_id, runner),
            PaperlessRunnerResetFailedDocumentsButton(entry.entry_id, runner),
            PaperlessRunnerShowLogButton(entry.entry_id, runner),
            PaperlessRunnerExportLogButton(entry.entry_id, runner),
        ],
        True,
    )


class PaperlessRunnerResetMetricsButton(ButtonEntity):
    """Button to reset token/cost metrics."""

    _attr_icon = "mdi:counter"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_reset_metrics"
        self._attr_name = "Paperless KIplus Statistiken zurücksetzen"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Ordnet den Button dem zentralen Integrationsgerät zu."""

        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Reset token/cost metrics."""

        await self._runner.async_reset_metrics()


class PaperlessRunnerBackfillButton(ButtonEntity):
    """Button to run a one-off enrichment backfill across existing documents."""

    _attr_icon = "mdi:database-refresh-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_start_backfill"
        self._attr_name = "Paperless KIplus Bestandsdaten neu anreichern"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Ordnet den Button dem zentralen Integrationsgerät zu."""

        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Start a background backfill run without blocking the HA UI."""

        self.hass.async_create_task(
            self._runner.async_run(backfill_existing_documents=True)
        )


class PaperlessRunnerStopButton(ButtonEntity):
    """Button to request a safe stop for the current run."""

    _attr_icon = "mdi:pause-circle-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_request_stop"
        self._attr_name = "Paperless KIplus Lauf pausieren"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Request a safe pause after the current document/batch."""

        await self._runner.async_request_stop()


class PaperlessRunnerHardStopButton(ButtonEntity):
    """Button to stop the current process immediately."""

    _attr_icon = "mdi:stop-circle-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_force_stop"
        self._attr_name = "Paperless KIplus Lauf sofort stoppen"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Terminate the active process immediately."""

        await self._runner.async_force_stop()


class PaperlessRunnerResumeButton(ButtonEntity):
    """Button to resume a paused run from its saved state."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_resume_run"
        self._attr_name = "Paperless KIplus Pausierten Lauf fortsetzen"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Resume the saved paused run in the background."""

        self.hass.async_create_task(self._runner.async_resume())


class PaperlessRunnerExportLogButton(ButtonEntity):
    """Button to export last log for easy support sharing."""

    _attr_icon = "mdi:file-download-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_export_log"
        self._attr_name = "Paperless KIplus Letztes Protokoll exportieren"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Ordnet den Button dem zentralen Integrationsgerät zu."""

        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Export last run log to /config/www for browser download."""

        await self._runner.async_export_last_log()


class PaperlessRunnerShowLogButton(ButtonEntity):
    """Button to show last log content in HA persistent notification."""

    _attr_icon = "mdi:text-box-search-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_show_log"
        self._attr_name = "Paperless KIplus Letztes Protokoll anzeigen"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Ordnet den Button dem zentralen Integrationsgerät zu."""

        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Show the last log as persistent notification."""

        await self._runner.async_show_last_log()


class PaperlessRunnerResetFailedDocumentsButton(ButtonEntity):
    """Button to clear failed/quarantine state files."""

    _attr_icon = "mdi:restore-alert"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_reset_failed_documents"
        self._attr_name = "Paperless KIplus Fehlgeschlagene Dokumente zurücksetzen"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Ordnet den Button dem zentralen Integrationsgerät zu."""

        return _device_info(self._entry_id)

    async def async_press(self) -> None:
        """Clear failed/quarantine cache files."""

        await self._runner.async_reset_failed_documents()
