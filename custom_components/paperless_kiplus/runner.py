"""Script runner for Paperless KIplus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import re
import shlex
from typing import Sequence
import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_STATUS_UPDATED

_LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Result metadata for a script run."""

    status: str
    exit_code: int | None
    message: str


class PaperlessRunner:
    """Execute the configured Paperless KIplus command safely."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        command: str,
        workdir: str,
        cooldown_seconds: int,
        metrics_file: str,
        config_file: str,
        dry_run: bool,
        all_documents: bool,
        max_documents: int,
        managed_config_enabled: bool,
        managed_config_yaml: str,
        input_cost_per_1k_tokens_eur: float,
        output_cost_per_1k_tokens_eur: float,
    ) -> None:
        self.hass = hass
        self.command = command
        self.workdir = workdir
        self.cooldown_seconds = cooldown_seconds
        self.metrics_file = metrics_file
        self.config_file = config_file
        self.default_dry_run = dry_run
        self.default_all_documents = all_documents
        self.default_max_documents = max_documents
        self.managed_config_enabled = managed_config_enabled
        self.managed_config_yaml = managed_config_yaml
        self.input_cost_per_1k_tokens_eur = input_cost_per_1k_tokens_eur
        self.output_cost_per_1k_tokens_eur = output_cost_per_1k_tokens_eur

        self._lock = asyncio.Lock()
        self.running = False

        self.last_started: datetime | None = None
        self.last_finished: datetime | None = None
        self.last_exit_code: int | None = None
        self.last_status: str = "idle"
        self.last_message: str = "not started"
        self.last_stdout_tail: str = ""
        self.last_stderr_tail: str = ""
        self.last_summary_line: str = ""
        self.last_cost_line: str = ""
        self.last_log_combined: str = ""
        self.last_scanned: int = 0
        self.last_updated: int = 0
        self.last_skipped: int = 0
        self.last_failed: int = 0
        self.last_run_total_tokens: int = 0
        self.last_run_cost_eur: float = 0.0
        self.total_tokens: int = 0
        self.total_cost_eur: float = 0.0
        self.last_metrics_updated: datetime | None = None
        self.last_command_executed: str = ""

    @property
    def cooldown_until(self) -> datetime | None:
        """Return the next allowed run time if cooldown is active."""

        if self.last_finished is None:
            return None
        return self.last_finished + timedelta(seconds=self.cooldown_seconds)

    async def async_run(
        self,
        *,
        force: bool = False,
        config_file: str | None = None,
        dry_run: bool | None = None,
        all_documents: bool | None = None,
        max_documents: int | None = None,
    ) -> RunResult:
        """Run the command unless already running or in cooldown."""

        if self._lock.locked() and not force:
            self.last_status = "skipped_running"
            self.last_message = "run skipped because another run is active"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        now = datetime.now(UTC)
        cooldown_until = self.cooldown_until
        if not force and cooldown_until is not None and now < cooldown_until:
            self.last_status = "cooldown"
            self.last_message = f"run skipped due to cooldown until {cooldown_until.isoformat()}"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        async with self._lock:
            self.running = True
            self.last_started = datetime.now(UTC)
            self.last_status = "running"
            self.last_message = "script is running"
            self._notify()

            try:
                effective_config_file = config_file if config_file is not None else self.config_file
                effective_dry_run = self.default_dry_run if dry_run is None else dry_run
                effective_all_documents = (
                    self.default_all_documents if all_documents is None else all_documents
                )
                effective_max_documents = (
                    self.default_max_documents if max_documents is None else int(max_documents)
                )

                if self.managed_config_enabled:
                    await self._write_managed_config(effective_config_file)

                args = self._build_command(
                    config_file=effective_config_file,
                    dry_run=effective_dry_run,
                    all_documents=effective_all_documents,
                    max_documents=effective_max_documents,
                )
                if not args:
                    raise ValueError("configured command is empty")
                self.last_command_executed = shlex.join(args)

                process = await asyncio.create_subprocess_exec(
                    *args,
                    cwd=self.workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()

                self.last_stdout_tail = stdout.decode("utf-8", errors="replace")[-20000:]
                self.last_stderr_tail = stderr.decode("utf-8", errors="replace")[-20000:]
                combined_tail = f"{self.last_stdout_tail}\n{self.last_stderr_tail}"
                self.last_summary_line = self._extract_last_line(
                    combined_tail, "Fertig. Gescannt="
                )
                self.last_cost_line = self._extract_last_line(
                    combined_tail, "Kosten/Token:"
                )
                self.last_scanned, self.last_updated, self.last_skipped, self.last_failed = (
                    self._parse_summary_counts(self.last_summary_line)
                )
                self.last_log_combined = (
                    f"[STDOUT]\n{self.last_stdout_tail.strip()}\n\n[STDERR]\n{self.last_stderr_tail.strip()}"
                ).strip()
                self.last_exit_code = process.returncode

                if process.returncode == 0:
                    self.last_status = "success"
                    self.last_message = "script completed successfully"
                else:
                    self.last_status = "error"
                    self.last_message = f"script failed with exit code {process.returncode}"
                    # Bei Fehlern die Output-Tails aktiv loggen, damit
                    # Konfigurationsprobleme (z. B. [CONFIG-ERROR]) direkt
                    # im Home-Assistant-Log sichtbar sind.
                    _LOGGER.error(
                        "Paperless KIplus run failed | exit_code=%s | stdout_tail=%s | stderr_tail=%s",
                        process.returncode,
                        self.last_stdout_tail.strip() or "<empty>",
                        self.last_stderr_tail.strip() or "<empty>",
                    )

                _LOGGER.info(
                    "Paperless KIplus run finished | status=%s exit_code=%s",
                    self.last_status,
                    self.last_exit_code,
                )
            except Exception as exc:  # noqa: BLE001
                self.last_status = "error"
                self.last_message = f"runner exception: {exc}"
                self.last_exit_code = None
                self.last_stderr_tail = str(exc)
                if isinstance(exc, FileNotFoundError):
                    self.last_message = (
                        "runner exception: Datei/Befehl nicht gefunden. "
                        "Prüfe 'Befehl' und 'Arbeitsverzeichnis' in der Integration."
                    )
                _LOGGER.exception("Paperless KIplus run crashed: %s", exc)
            finally:
                await self._refresh_metrics_from_file()
                self.running = False
                self.last_finished = datetime.now(UTC)
                self._notify()

        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    def _notify(self) -> None:
        """Notify entities/sensors about runner state updates."""

        async_dispatcher_send(self.hass, SIGNAL_STATUS_UPDATED)

    async def _refresh_metrics_from_file(self) -> None:
        """Load token/cost metrics from the configured JSON file."""

        path = Path(self.metrics_file)
        if not path.is_absolute():
            path = Path(self.workdir) / path

        if not path.exists():
            return

        try:
            payload = await self.hass.async_add_executor_job(
                lambda: json.loads(path.read_text(encoding="utf-8"))
            )
            last = payload.get("last_run") or {}
            totals = payload.get("totals") or {}

            self.last_run_total_tokens = int(last.get("total_tokens", 0) or 0)
            self.last_run_cost_eur = float(last.get("cost_eur", 0.0) or 0.0)
            self.total_tokens = int(totals.get("total_tokens", 0) or 0)
            self.total_cost_eur = float(totals.get("cost_eur", 0.0) or 0.0)
            self.last_metrics_updated = datetime.now(UTC)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _LOGGER.warning("Could not parse metrics file '%s': %s", path, exc)

    async def async_load_initial_metrics(self) -> None:
        """Lädt Metriken beim Setup, damit Sensorwerte nach Reload erhalten bleiben."""

        await self._refresh_metrics_from_file()

    async def async_reset_metrics(self) -> None:
        """Setzt Token-/Kostenmetriken in Datei und Runtime zurück."""

        path = Path(self.metrics_file)
        if not path.is_absolute():
            path = Path(self.workdir) / path

        payload = {
            "last_run": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_eur": 0.0,
                "finished_at": None,
                "model": None,
            },
            "totals": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_eur": 0.0,
                "runs": 0,
            },
        }

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        await self.hass.async_add_executor_job(_write)

        self.last_run_total_tokens = 0
        self.last_run_cost_eur = 0.0
        self.total_tokens = 0
        self.total_cost_eur = 0.0
        self.last_metrics_updated = datetime.now(UTC)
        self.last_status = "metrics_reset"
        self.last_message = "token/cost metrics reset"
        self._notify()

    @staticmethod
    def _extract_last_line(text: str, marker: str) -> str:
        """Extract the most recent line containing a marker from a log text."""

        for line in reversed(text.splitlines()):
            if marker in line:
                return line.strip()
        return ""

    @staticmethod
    def _parse_summary_counts(summary_line: str) -> tuple[int, int, int, int]:
        """Parse Lauf-Zählwerte aus der Fertig-Zeile.

        Erwartetes Format:
        Fertig. Gescannt=5, Aktualisiert=2, Übersprungen=2, Fehler=1
        """

        if not summary_line:
            return (0, 0, 0, 0)
        match = re.search(
            r"Gescannt=(\d+),\s*Aktualisiert=(\d+),\s*Übersprungen=(\d+),\s*Fehler=(\d+)",
            summary_line,
        )
        if not match:
            return (0, 0, 0, 0)
        return tuple(int(group) for group in match.groups())  # type: ignore[return-value]

    async def _write_managed_config(self, config_file: str) -> None:
        """Write integration-managed YAML config to disk before script execution.

        Damit wird die Konfiguration nativ in Home Assistant gepflegt und
        für jeden Lauf konsistent in die vom Skript erwartete YAML-Datei geschrieben.
        """

        if not self.managed_config_yaml.strip():
            raise ValueError(
                "managed_config_enabled ist aktiv, aber managed_config_yaml ist leer."
            )

        path = Path(config_file)
        if not path.is_absolute():
            path = Path(self.workdir) / path
        raw_yaml = self.managed_config_yaml

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            parsed = yaml.safe_load(raw_yaml) or {}
            if isinstance(parsed, dict):
                parsed["input_cost_per_1k_tokens_eur"] = float(self.input_cost_per_1k_tokens_eur)
                parsed["output_cost_per_1k_tokens_eur"] = float(self.output_cost_per_1k_tokens_eur)
                content = yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False)
            else:
                content = raw_yaml
            path.write_text(content, encoding="utf-8")

        await self.hass.async_add_executor_job(_write)

    def _build_command(
        self,
        *,
        config_file: str,
        dry_run: bool,
        all_documents: bool,
        max_documents: int,
    ) -> list[str]:
        """Build a robust CLI command based on HA options and per-run overrides.

        Falls der Basis-Befehl Flags bereits enthält, werden sie nicht doppelt
        angehängt. So bleibt auch eine manuell gepflegte Kommandozeile kompatibel.
        """

        args = shlex.split(self.command)
        if not args:
            return []

        def _has_flag(names: Sequence[str]) -> bool:
            return any(flag in args for flag in names)

        if config_file and not _has_flag(["--config"]):
            args.extend(["--config", config_file])
        if dry_run and not _has_flag(["--dry-run"]):
            args.append("--dry-run")
        if all_documents and not _has_flag(["--all-documents"]):
            args.append("--all-documents")
        if max_documents > 0 and not _has_flag(["--max-documents"]):
            args.extend(["--max-documents", str(max_documents)])

        return args
