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
    """Set up sensors from config entry."""

    runner: PaperlessRunner = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            PaperlessRunnerStatusSensor(entry.entry_id, runner),
            PaperlessRunnerRunLogSensor(entry.entry_id, runner),
            PaperlessRunnerLastTokensSensor(entry.entry_id, runner),
            PaperlessRunnerLastCostSensor(entry.entry_id, runner),
            PaperlessRunnerTotalTokensSensor(entry.entry_id, runner),
            PaperlessRunnerTotalCostSensor(entry.entry_id, runner),
        ],
        True,
    )


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
    def extra_state_attributes(self) -> dict[str, str | int | float | None]:
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
            "last_command_executed": self._runner.last_command_executed,
            "workdir": self._runner.workdir,
            "config_file": self._runner.config_file,
            "default_dry_run": self._runner.default_dry_run,
            "default_all_documents": self._runner.default_all_documents,
            "default_max_documents": self._runner.default_max_documents,
            "managed_config_enabled": self._runner.managed_config_enabled,
            "managed_config_yaml_chars": len(self._runner.managed_config_yaml or ""),
            "input_cost_per_1k_tokens_eur": self._runner.input_cost_per_1k_tokens_eur,
            "output_cost_per_1k_tokens_eur": self._runner.output_cost_per_1k_tokens_eur,
            "metrics_file": self._runner.metrics_file,
            "stdout_tail": self._runner.last_stdout_tail,
            "stderr_tail": self._runner.last_stderr_tail,
            "summary_line": self._runner.last_summary_line,
            "cost_line": self._runner.last_cost_line,
            "last_run_total_tokens": self._runner.last_run_total_tokens,
            "last_run_cost_eur": round(self._runner.last_run_cost_eur, 6),
            "total_tokens": self._runner.total_tokens,
            "total_cost_eur": round(self._runner.total_cost_eur, 6),
        }


class PaperlessRunnerRunLogSensor(SensorEntity):
    """Expose the latest run log and summary as dedicated entity attributes."""

    _attr_icon = "mdi:text-box-search-outline"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_run_log"
        self._attr_name = "Paperless KIplus Letztes Protokoll"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATUS_UPDATED,
                self.async_write_ha_state,
            )
        )

    @property
    def native_value(self) -> str:
        """Use the current run status as compact state value."""

        return self._runner.last_status

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Detailed log payload for troubleshooting and post-run review."""

        return {
            "last_message": self._runner.last_message,
            "summary_line": self._runner.last_summary_line or None,
            "cost_line": self._runner.last_cost_line or None,
            "log_text": self._runner.last_log_combined or None,
            "stdout_tail": self._runner.last_stdout_tail or None,
            "stderr_tail": self._runner.last_stderr_tail or None,
        }


class _BaseMetricSensor(SensorEntity):
    """Shared base sensor for runner metrics."""

    def __init__(self, entry_id: str, runner: PaperlessRunner, *, suffix: str, name: str) -> None:
        self._entry_id = entry_id
        self._runner = runner
        self._attr_unique_id = f"{entry_id}_{suffix}"
        self._attr_name = name

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_STATUS_UPDATED,
                self.async_write_ha_state,
            )
        )


class PaperlessRunnerLastTokensSensor(_BaseMetricSensor):
    """Last run token usage."""

    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "tokens"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        super().__init__(
            entry_id,
            runner,
            suffix="last_run_tokens",
            name="Paperless KIplus Letzter Lauf Tokens",
        )

    @property
    def native_value(self) -> int:
        return self._runner.last_run_total_tokens


class PaperlessRunnerLastCostSensor(_BaseMetricSensor):
    """Last run EUR cost."""

    _attr_icon = "mdi:currency-eur"
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        super().__init__(
            entry_id,
            runner,
            suffix="last_run_cost",
            name="Paperless KIplus Letzter Lauf Kosten",
        )

    @property
    def native_value(self) -> float:
        return round(self._runner.last_run_cost_eur, 6)


class PaperlessRunnerTotalTokensSensor(_BaseMetricSensor):
    """Total token usage across all runs."""

    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "tokens"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        super().__init__(
            entry_id,
            runner,
            suffix="total_tokens",
            name="Paperless KIplus Gesamt Tokens",
        )

    @property
    def native_value(self) -> int:
        return self._runner.total_tokens


class PaperlessRunnerTotalCostSensor(_BaseMetricSensor):
    """Total EUR cost across all runs."""

    _attr_icon = "mdi:currency-eur"
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, entry_id: str, runner: PaperlessRunner) -> None:
        super().__init__(
            entry_id,
            runner,
            suffix="total_cost",
            name="Paperless KIplus Gesamtkosten",
        )

    @property
    def native_value(self) -> float:
        return round(self._runner.total_cost_eur, 6)
