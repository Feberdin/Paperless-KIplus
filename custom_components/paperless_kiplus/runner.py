"""Script runner for Paperless KIplus.

Purpose:
- Startet das eigentliche CLI-Skript sicher aus Home Assistant.
- Stellt Live-Fortschritt, Pause/Resume und automatische Wiederaufnahme bei
  Provider-Wartezeiten bereit.

Input / Output:
- Input: Integrationsoptionen, Startparameter und Runtime-Events aus dem
  Paperless-CLI-Skript.
- Output: aktualisierte Sensor-/Button-Zustände, laufende Prozesssteuerung und
  persistente Resume-Dateien im Arbeitsverzeichnis.

Important invariants:
- Ein manueller Stop ist ein kontrollierter "Pause nach sicherem Punkt", kein
  harter Kill des Prozesses.
- Ein Sofort-Stopp beendet den Prozess aktiv. Wenn bereits Fortschrittsevents
  vorliegen, bleibt daraus ein Resume-Zustand erhalten.
- Eine Provider-Pause (429 / Quota / Retry-After) bleibt als Resume-State
  erhalten und kann automatisch oder manuell fortgesetzt werden.
- Dry-Run, Backfill und normale Läufe nutzen denselben Runner-Pfad.

How to debug:
- Prüfe `sensor.paperless_kiplus_status` auf `progress_*`, `pause_reason`,
  `auto_resume_at` und `resume_available`.
- `stdout_tail` und `stderr_tail` zeigen die letzten Logzeilen bereits während
  des Laufs.
- Die Runtime-State-Datei liegt im Arbeitsverzeichnis und heißt pro Entry
  `.paperless_kiplus_<entry_id>_run_state.json`.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
import re
import shlex
from typing import Any, Optional, Sequence

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .config_export import build_effective_managed_config_yaml
from .const import SIGNAL_STATUS_UPDATED

_LOGGER = logging.getLogger(__name__)

RUNTIME_EVENT_MARKER = "PAPERLESS_RUNTIME_EVENT "
RUN_PAUSE_EXIT_CODE = 75
TAIL_LIMIT_CHARS = 20000
FORCE_STOP_GRACE_SECONDS = 5.0


def build_paperless_document_url(base_url: str, document_id: int | None) -> str:
    """Builds a Paperless document detail URL from base URL and document id.

    Why this exists:
    - The Home Assistant UI should offer stable, shareable links to the current
      and last completed document.
    - The Paperless detail route is centralized here so we can adapt it in one
      place if upstream frontend routing changes in the future.
    """

    normalized_base = str(base_url or "").strip().rstrip("/")
    if not normalized_base or document_id is None:
        return ""
    return f"{normalized_base}/documents/{int(document_id)}/details"


def infer_restart_backfill_mode(
    source_payload: dict[str, Any] | None,
    *,
    explicit_backfill: bool | None = None,
) -> bool:
    """Infers whether a fresh restart should continue in backfill mode.

    Why this exists:
    - Users expect "Neustart" to begin fresh, but usually in the same run mode
      as the currently paused or active run.
    - A pure helper keeps this decision easy to test and avoids scattering mode
      inference logic across the runner.
    """

    if explicit_backfill is not None:
        return bool(explicit_backfill)
    payload = source_payload if isinstance(source_payload, dict) else {}
    mode = payload.get("mode") or {}
    if not isinstance(mode, dict):
        return False
    return bool(mode.get("backfill_existing_documents", False))


@dataclass
class RunResult:
    """Result metadata for a script run."""

    status: str
    exit_code: int | None
    message: str


def build_force_stop_resume_payload(
    source_payload: dict[str, Any],
    *,
    paused_at: datetime | None = None,
) -> dict[str, Any]:
    """Leitet aus dem letzten Fortschritt einen Resume-Zustand für Sofort-Stopps ab.

    Warum diese Hilfsfunktion existiert:
    - Ein harter Stop beendet den CLI-Prozess außerhalb seines normalen
      Pause-Pfads.
    - Damit ein späteres Resume trotzdem möglich bleibt, konservieren wir den
      letzten bekannten Fortschrittszustand und markieren ihn explizit als
      `force_stop`.

    Beispiel:
    - Input: `{"kind": "progress", "progress": {"completed_documents": 12}}`
    - Output: derselbe Stand, aber mit `status="paused"` und
      `pause_reason="force_stop"`.
    """

    if not isinstance(source_payload, dict) or not source_payload:
        return {}

    payload = dict(source_payload)
    payload.pop("kind", None)
    payload["status"] = "paused"
    payload["pause_reason"] = "force_stop"
    payload["retry_after_seconds"] = None
    payload["updated_at"] = (paused_at or datetime.now(UTC)).isoformat()
    return payload


class PaperlessRunner:
    """Execute the configured Paperless KIplus command safely."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str,
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
        already_classified_skip: bool,
        already_classified_require_ki_tag: bool,
        precheck_min_content_chars: int,
        precheck_min_word_count: int,
        precheck_min_alnum_ratio: float,
        precheck_blocked_filename_patterns: str,
        precheck_image_only_gate: bool,
        precheck_duplicate_hash_gate: bool,
        precheck_duplicate_apply_metadata: bool,
        reprocess_ki_tagged_documents: bool,
        enable_parallel_ai: bool,
        max_parallel_ai_jobs: int,
        enable_tax_enrichment: bool,
        tax_process_ki_tagged_documents: bool,
        tax_personal_context: str,
    ) -> None:
        self.hass = hass
        self.entry_id = entry_id
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
        self.already_classified_skip = already_classified_skip
        self.already_classified_require_ki_tag = already_classified_require_ki_tag
        self.precheck_min_content_chars = precheck_min_content_chars
        self.precheck_min_word_count = precheck_min_word_count
        self.precheck_min_alnum_ratio = precheck_min_alnum_ratio
        self.precheck_blocked_filename_patterns = precheck_blocked_filename_patterns
        self.precheck_image_only_gate = precheck_image_only_gate
        self.precheck_duplicate_hash_gate = precheck_duplicate_hash_gate
        self.precheck_duplicate_apply_metadata = precheck_duplicate_apply_metadata
        self.reprocess_ki_tagged_documents = reprocess_ki_tagged_documents
        self.enable_parallel_ai = enable_parallel_ai
        self.max_parallel_ai_jobs = max_parallel_ai_jobs
        self.enable_tax_enrichment = enable_tax_enrichment
        self.tax_process_ki_tagged_documents = tax_process_ki_tagged_documents
        self.tax_personal_context = tax_personal_context
        self.execution_mode = "local"
        self.remote_worker_url = ""
        self.remote_worker_token = ""
        self.remote_worker_verify_ssl = True
        self.remote_worker_sync_config = False
        self.worker_ui_url = ""
        self.last_config_sync_at: datetime | None = None
        self.last_config_sync_status = "not_applicable"

        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._auto_resume_task: asyncio.Task | None = None
        self._force_stop_task: asyncio.Task | None = None
        self._latest_runtime_state_payload: dict[str, Any] = {}
        self.running = False
        self.stop_requested = False
        self.force_stop_requested = False
        self.resume_available = False
        self.pause_reason: str = ""
        self.auto_resume_at: datetime | None = None

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
        self.last_run_bypass_skipped: int = 0
        self.total_tokens: int = 0
        self.total_cost_eur: float = 0.0
        self.total_bypass_skipped: int = 0
        self.last_metrics_updated: datetime | None = None
        self.last_command_executed: str = ""
        self.last_log_export_path: str = ""
        self.last_log_export_url: str = ""
        self.active_quarantine_count: int = 0
        self.active_bypass_count: int = 0

        self.progress_total_documents: int = 0
        self.progress_completed_documents: int = 0
        self.progress_percent: float = 0.0
        self.progress_scanned: int = 0
        self.progress_updated: int = 0
        self.progress_skipped: int = 0
        self.progress_failed: int = 0
        self.progress_bypassed: int = 0
        self.progress_bypass_skipped: int = 0
        self.progress_prefiltered_ki_tagged: int = 0
        self.progress_current_document_id: int | None = None
        self.progress_current_document_title: str = ""
        self.progress_budget_used: int = 0
        self.progress_pending_documents: int = 0
        self.progress_last_event_at: datetime | None = None
        self.paperless_base_url: str = ""
        self.progress_current_document_url: str = ""
        self.last_completed_document_id: int | None = None
        self.last_completed_document_title: str = ""
        self.last_completed_document_url: str = ""
        self.last_completed_document_at: datetime | None = None

        self._refresh_paperless_base_url()

    @property
    def cooldown_until(self) -> datetime | None:
        """Return the next allowed run time if cooldown is active."""

        if self.last_finished is None:
            return None
        return self.last_finished + timedelta(seconds=self.cooldown_seconds)

    @property
    def run_state_path(self) -> str:
        """Exposes the resolved run-state path for diagnostics."""

        return str(self._run_state_path())

    @property
    def stop_request_path(self) -> str:
        """Exposes the resolved stop-request path for diagnostics."""

        return str(self._stop_request_path())

    def _run_state_path(self) -> Path:
        """Returns the persisted resume-state path for this config entry."""

        return Path(self.workdir) / f".paperless_kiplus_{self.entry_id}_run_state.json"

    def _stop_request_path(self) -> Path:
        """Returns the stop-request marker path for this config entry."""

        return Path(self.workdir) / f".paperless_kiplus_{self.entry_id}_stop.request"

    async def async_run(
        self,
        *,
        force: bool = False,
        config_file: str | None = None,
        dry_run: bool | None = None,
        all_documents: bool | None = None,
        max_documents: int | None = None,
        backfill_existing_documents: bool = False,
        resume_run: bool = False,
    ) -> RunResult:
        """Run the command unless already running or in cooldown."""

        if self._lock.locked() and not force:
            self.last_status = "skipped_running"
            self.last_message = "run skipped because another run is active"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        now = datetime.now(UTC)
        cooldown_until = self.cooldown_until
        if not resume_run and not force and cooldown_until is not None and now < cooldown_until:
            self.last_status = "cooldown"
            self.last_message = f"run skipped due to cooldown until {cooldown_until.isoformat()}"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        if resume_run and not self._run_state_path().exists():
            self.last_status = "resume_unavailable"
            self.last_message = "resume requested but no paused run state exists"
            self.resume_available = False
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        self._cancel_auto_resume_task()
        self._cancel_force_stop_task()

        async with self._lock:
            self.running = True
            self.stop_requested = False
            self.force_stop_requested = False
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            self._latest_runtime_state_payload = {}
            self.last_started = datetime.now(UTC)
            self.last_status = "running"
            self.last_message = "paused run is resuming" if resume_run else "script is running"
            self.last_stdout_tail = ""
            self.last_stderr_tail = ""
            self.last_summary_line = ""
            self.last_cost_line = ""
            self.last_log_combined = ""
            self.progress_current_document_url = ""
            if not resume_run:
                self.last_completed_document_id = None
                self.last_completed_document_title = ""
                self.last_completed_document_url = ""
                self.last_completed_document_at = None
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
                self._refresh_paperless_base_url(effective_config_file)

                await self.hass.async_add_executor_job(
                    lambda: self._delete_file(self._stop_request_path())
                )

                args = self._build_command(
                    config_file=effective_config_file,
                    dry_run=effective_dry_run,
                    all_documents=effective_all_documents,
                    max_documents=effective_max_documents,
                    backfill_existing_documents=backfill_existing_documents,
                    resume_run=resume_run,
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
                self._process = process

                stream_tasks = [
                    self.hass.async_create_task(self._stream_reader(process.stdout, is_stderr=False)),
                    self.hass.async_create_task(self._stream_reader(process.stderr, is_stderr=True)),
                ]

                self.last_exit_code = await process.wait()
                await asyncio.gather(*stream_tasks)
                self._rebuild_combined_log()

                if self.last_exit_code == 0:
                    self.last_status = "success"
                    self.last_message = "script completed successfully"
                    self.resume_available = False
                    self.pause_reason = ""
                    self.auto_resume_at = None
                    self.progress_current_document_title = ""
                    self.progress_current_document_id = None
                    self.progress_current_document_url = ""
                elif self.last_exit_code == RUN_PAUSE_EXIT_CODE:
                    await self._refresh_resume_state()
                    if self.pause_reason == "manual_stop":
                        self.last_status = "paused"
                        self.last_message = "run paused manually; resume available"
                    else:
                        self.last_status = "waiting_auto_resume"
                        self.last_message = (
                            f"run paused due to {self.pause_reason or 'provider backoff'}"
                        )
                        if self.auto_resume_at is not None:
                            self.last_message += (
                                f" until {self.auto_resume_at.isoformat()}"
                            )
                            self._schedule_auto_resume()
                elif self.force_stop_requested:
                    await self._refresh_resume_state()
                    self.last_status = "force_stopped"
                    self.auto_resume_at = None
                    self.pause_reason = self.pause_reason or "force_stop"
                    if self.resume_available:
                        self.last_message = (
                            "run stopped immediately; resume available from last saved progress"
                        )
                    else:
                        self.last_message = (
                            "run stopped immediately; no resume state was available yet"
                        )
                else:
                    self.last_status = "error"
                    self.last_message = f"script failed with exit code {self.last_exit_code}"
                    _LOGGER.error(
                        "Paperless KIplus run failed | exit_code=%s | stdout_tail=%s | stderr_tail=%s",
                        self.last_exit_code,
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
                self._rebuild_combined_log()
                if isinstance(exc, FileNotFoundError):
                    self.last_message = (
                        "runner exception: Datei/Befehl nicht gefunden. "
                        "Prüfe 'Befehl' und 'Arbeitsverzeichnis' in der Integration."
                    )
                _LOGGER.exception("Paperless KIplus run crashed: %s", exc)
            finally:
                self._cancel_force_stop_task()
                self._process = None
                await self._refresh_metrics_from_file()
                await self._refresh_failed_state_counts()
                if self.last_exit_code != RUN_PAUSE_EXIT_CODE and not self._run_state_path().exists():
                    self.resume_available = False
                    self.pause_reason = ""
                self.running = False
                self.stop_requested = False
                self.force_stop_requested = False
                self.last_finished = datetime.now(UTC)
                self._notify()

        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_request_stop(self) -> RunResult:
        """Requests a safe stop at the next document/batch boundary."""

        if not self.running:
            self.last_status = "stop_ignored"
            self.last_message = "no active run to stop"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        await self.hass.async_add_executor_job(
            lambda: self._write_json_file(
                self._stop_request_path(),
                {
                    "requested_at": datetime.now(UTC).isoformat(),
                    "reason": "manual_stop",
                },
            )
        )
        self.stop_requested = True
        self.last_status = "stop_requested"
        self.last_message = "stop requested; runner pauses after current document/batch"
        self._notify()
        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_force_stop(self) -> RunResult:
        """Beendet den Prozess aktiv und bewahrt nach Möglichkeit einen Resume-Stand."""

        process = self._process
        if not self.running or process is None or process.returncode is not None:
            self.last_status = "stop_ignored"
            self.last_message = "no active run to stop immediately"
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

        self._cancel_auto_resume_task()
        self._cancel_force_stop_task()
        await self.hass.async_add_executor_job(self._persist_force_stop_resume_state)
        await self.hass.async_add_executor_job(
            lambda: self._delete_file(self._stop_request_path())
        )

        self.stop_requested = True
        self.force_stop_requested = True
        self.last_status = "stop_now_requested"
        self.last_message = "immediate stop requested; terminating active process"
        self._notify()

        with contextlib.suppress(ProcessLookupError):
            process.terminate()

        self._force_stop_task = self.hass.async_create_task(
            self._force_stop_after_grace(process)
        )
        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_resume(self, *, force: bool = True) -> RunResult:
        """Resumes a previously paused run from its persisted state."""

        return await self.async_run(force=force, resume_run=True)

    async def async_restart(
        self,
        *,
        force: bool = True,
        backfill_existing_documents: bool | None = None,
    ) -> RunResult:
        """Starts a fresh run and discards any old resume state.

        Behavior:
        - active runs are stopped immediately first
        - paused resume state is explicitly deleted
        - the new run starts from the beginning, not via `--resume-run`
        - if no mode override is given, we reuse the last known run mode
        """

        base_payload = self._read_json_file(self._run_state_path())
        if not base_payload:
            base_payload = dict(self._latest_runtime_state_payload)
        restart_backfill = infer_restart_backfill_mode(
            base_payload,
            explicit_backfill=backfill_existing_documents,
        )

        self.last_status = "restart_requested"
        self.last_message = (
            "fresh restart requested; previous resume state will be discarded"
        )
        self._notify()

        if self.running:
            await self.async_force_stop()
            stopped = await self._wait_for_runner_to_stop()
            if not stopped:
                self.last_status = "restart_failed"
                self.last_message = (
                    "restart requested, but the previous process did not stop in time"
                )
                self._notify()
                return RunResult(self.last_status, self.last_exit_code, self.last_message)

        await self.hass.async_add_executor_job(self._clear_restart_state_files)
        self._latest_runtime_state_payload = {}
        self.resume_available = False
        self.pause_reason = ""
        self.auto_resume_at = None
        self.progress_pending_documents = 0
        self.progress_current_document_id = None
        self.progress_current_document_title = ""
        self.progress_current_document_url = ""
        self._notify()

        return await self.async_run(
            force=force,
            backfill_existing_documents=restart_backfill,
            resume_run=False,
        )

    async def async_shutdown(self) -> None:
        """Cleans up background resume tasks when the integration unloads."""

        self._cancel_auto_resume_task()

    def _notify(self) -> None:
        """Notify entities/sensors about runner state updates."""

        async_dispatcher_send(self.hass, SIGNAL_STATUS_UPDATED)

    async def _wait_for_runner_to_stop(self, timeout_seconds: float = 45.0) -> bool:
        """Waits until the active runner fully stops after a force stop request."""

        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        while self.running or self._lock.locked():
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.2)
        return True

    async def _stream_reader(
        self,
        stream: asyncio.StreamReader | None,
        *,
        is_stderr: bool,
    ) -> None:
        """Reads subprocess output line by line for live progress updates."""

        if stream is None:
            return

        while True:
            raw = await stream.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            self._append_output_line(line, is_stderr=is_stderr)

    def _append_output_line(self, line: str, *, is_stderr: bool) -> None:
        """Stores the most recent output lines and parses runtime events."""

        if is_stderr:
            self.last_stderr_tail = self._append_tail(self.last_stderr_tail, line)
        else:
            self.last_stdout_tail = self._append_tail(self.last_stdout_tail, line)

        runtime_event = self._extract_runtime_event(line)
        if runtime_event is not None:
            self._apply_runtime_event(runtime_event)
            self._rebuild_combined_log()
            self._notify()
            return

        if "Fertig. Gescannt=" in line:
            self.last_summary_line = line.strip()
            (
                self.last_scanned,
                self.last_updated,
                self.last_skipped,
                self.last_failed,
            ) = self._parse_summary_counts(self.last_summary_line)
        elif "Kosten/Token:" in line:
            self.last_cost_line = line.strip()

        self._rebuild_combined_log()

    def _load_yaml_mapping(self, yaml_text: str, *, source_name: str) -> dict[str, Any]:
        """Parses a YAML config defensively and returns a mapping.

        Why this exists:
        - Document links should reuse the same `paperless_url` as the actual run.
        - Broken YAML should not break the HA integration; it should only
          disable the convenience links and log a clear hint.
        """

        try:
            payload = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            _LOGGER.warning("Could not parse %s for Paperless links: %s", source_name, exc)
            return {}
        return payload if isinstance(payload, dict) else {}

    def _load_config_mapping(self, config_file: str | None = None) -> dict[str, Any]:
        """Loads the effective YAML config to resolve the Paperless base URL."""

        if self.managed_config_enabled and str(self.managed_config_yaml or "").strip():
            payload = self._load_yaml_mapping(
                self.managed_config_yaml,
                source_name="managed YAML config",
            )
            if payload:
                return payload

        config_path = Path(config_file or self.config_file or "")
        if not str(config_path).strip():
            return {}
        if not config_path.is_absolute():
            config_path = Path(self.workdir) / config_path
        try:
            if not config_path.exists():
                return {}
            return self._load_yaml_mapping(
                config_path.read_text(encoding="utf-8"),
                source_name=f"config file {config_path}",
            )
        except OSError as exc:
            _LOGGER.warning("Could not read config file %s for Paperless links: %s", config_path, exc)
            return {}

    def _refresh_paperless_base_url(self, config_file: str | None = None) -> None:
        """Refreshes cached Paperless base URL from the effective config."""

        payload = self._load_config_mapping(config_file)
        self.paperless_base_url = str(payload.get("paperless_url") or "").strip().rstrip("/")
        self.progress_current_document_url = build_paperless_document_url(
            self.paperless_base_url,
            self.progress_current_document_id,
        )
        self.last_completed_document_url = build_paperless_document_url(
            self.paperless_base_url,
            self.last_completed_document_id,
        )

    def _extract_runtime_event(self, line: str) -> dict[str, Any] | None:
        """Parses machine-readable progress events emitted by the CLI script."""

        if RUNTIME_EVENT_MARKER not in line:
            return None
        payload_text = line.split(RUNTIME_EVENT_MARKER, 1)[1].strip()
        if not payload_text:
            return None
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            _LOGGER.warning("Could not parse runtime event payload: %s", payload_text)
            return None
        return payload if isinstance(payload, dict) else None

    def _apply_runtime_event(self, payload: dict[str, Any]) -> None:
        """Maps CLI runtime events into Home Assistant visible runner state."""

        self._latest_runtime_state_payload = {
            key: value for key, value in payload.items() if key != "kind"
        }
        progress = payload.get("progress") or {}
        current_document = payload.get("current_document") or {}
        self.progress_total_documents = int(progress.get("total_documents", 0) or 0)
        self.progress_completed_documents = int(progress.get("completed_documents", 0) or 0)
        self.progress_percent = float(progress.get("percent", 0.0) or 0.0)
        self.progress_scanned = int(progress.get("scanned", 0) or 0)
        self.progress_updated = int(progress.get("updated", 0) or 0)
        self.progress_skipped = int(progress.get("skipped", 0) or 0)
        self.progress_failed = int(progress.get("failed", 0) or 0)
        self.progress_bypassed = int(progress.get("bypassed", 0) or 0)
        self.progress_bypass_skipped = int(progress.get("bypass_skipped", 0) or 0)
        self.progress_prefiltered_ki_tagged = int(
            progress.get("prefilt_ki_tagged", 0) or 0
        )
        self.progress_budget_used = int(progress.get("budget_used", 0) or 0)
        self.progress_pending_documents = len(payload.get("pending_documents") or [])
        self.progress_current_document_id = self._safe_int(current_document.get("id"))
        self.progress_current_document_title = str(current_document.get("title") or "")
        updated_at_raw = payload.get("updated_at")
        if isinstance(updated_at_raw, str) and updated_at_raw.strip():
            with contextlib.suppress(ValueError):
                self.progress_last_event_at = datetime.fromisoformat(updated_at_raw)
        if self.progress_last_event_at is None:
            self.progress_last_event_at = datetime.now(UTC)
        self.progress_current_document_url = build_paperless_document_url(
            self.paperless_base_url,
            self.progress_current_document_id,
        )
        self.last_scanned = self.progress_scanned
        self.last_updated = self.progress_updated
        self.last_skipped = self.progress_skipped
        self.last_failed = self.progress_failed

        completed_ids = {
            self._safe_int(doc_id)
            for doc_id in (payload.get("completed_document_ids") or [])
        }
        if (
            self.progress_current_document_id is not None
            and self.progress_current_document_id in completed_ids
        ):
            self.last_completed_document_id = self.progress_current_document_id
            self.last_completed_document_title = self.progress_current_document_title
            self.last_completed_document_url = build_paperless_document_url(
                self.paperless_base_url,
                self.last_completed_document_id,
            )
            self.last_completed_document_at = self.progress_last_event_at

        kind = str(payload.get("kind") or "")
        status = str(payload.get("status") or "")
        self.pause_reason = str(payload.get("pause_reason") or self.pause_reason or "")
        retry_after_seconds = self._safe_float(payload.get("retry_after_seconds"))
        if kind == "paused":
            self.resume_available = True
            if retry_after_seconds is not None:
                self.auto_resume_at = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
            else:
                self.auto_resume_at = None
        elif status == "success":
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            self.progress_pending_documents = 0

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
            self.last_run_bypass_skipped = int(last.get("bypass_skipped", 0) or 0)
            self.total_tokens = int(totals.get("total_tokens", 0) or 0)
            self.total_cost_eur = float(totals.get("cost_eur", 0.0) or 0.0)
            self.total_bypass_skipped = int(totals.get("bypass_skipped", 0) or 0)
            self.last_metrics_updated = datetime.now(UTC)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            _LOGGER.warning("Could not parse metrics file '%s': %s", path, exc)

    async def _refresh_failed_state_counts(self) -> None:
        """Lädt aktive Quarantäne-/Bypass-Anzahl aus den State-Dateien."""

        config_payload: dict = {}
        if self.managed_config_enabled and self.managed_config_yaml.strip():
            try:
                parsed = yaml.safe_load(self.managed_config_yaml) or {}
                if isinstance(parsed, dict):
                    config_payload = parsed
            except yaml.YAMLError:
                config_payload = {}
        else:
            config_path = Path(self.config_file)
            if not config_path.is_absolute():
                config_path = Path(self.workdir) / config_path
            if config_path.exists():
                try:
                    parsed = await self.hass.async_add_executor_job(
                        lambda: yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                    )
                    if isinstance(parsed, dict):
                        config_payload = parsed
                except (OSError, yaml.YAMLError):
                    config_payload = {}

        failed_docs_name = str(
            config_payload.get("failed_documents_file", "failed_documents.json")
        ).strip()
        bypass_name = str(
            config_payload.get("tag_bypass_file", "tag_bypass_documents.json")
        ).strip()

        failed_docs_path = Path(failed_docs_name or "failed_documents.json")
        if not failed_docs_path.is_absolute():
            failed_docs_path = Path(self.workdir) / failed_docs_path
        bypass_path = Path(bypass_name or "tag_bypass_documents.json")
        if not bypass_path.is_absolute():
            bypass_path = Path(self.workdir) / bypass_path

        def _read_json(path: Path) -> dict:
            if not path.exists():
                return {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}

        failed_payload, bypass_payload = await asyncio.gather(
            self.hass.async_add_executor_job(_read_json, failed_docs_path),
            self.hass.async_add_executor_job(_read_json, bypass_path),
        )

        now_ts = datetime.now(UTC).timestamp()
        quarantine_count = 0
        for _, value in failed_payload.items():
            try:
                if float(value) > now_ts:
                    quarantine_count += 1
            except (TypeError, ValueError):
                continue

        self.active_quarantine_count = quarantine_count
        self.active_bypass_count = len(bypass_payload)

    async def _refresh_resume_state(self) -> None:
        """Loads persisted run-state metadata for paused/resumable runs."""

        path = self._run_state_path()
        if not path.exists():
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            return

        def _read_state() -> dict[str, Any]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}

        payload = await self.hass.async_add_executor_job(_read_state)
        if not payload:
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            return

        self._latest_runtime_state_payload = dict(payload)
        self._apply_runtime_event({"kind": "paused", **payload})
        self.resume_available = True
        self.pause_reason = str(payload.get("pause_reason") or self.pause_reason or "")
        retry_after_seconds = self._safe_float(payload.get("retry_after_seconds"))
        updated_at = self._parse_datetime(payload.get("updated_at"))
        if retry_after_seconds is not None and updated_at is not None:
            self.auto_resume_at = updated_at + timedelta(seconds=retry_after_seconds)
        elif retry_after_seconds is not None:
            self.auto_resume_at = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
        else:
            self.auto_resume_at = None

    async def async_load_initial_metrics(self) -> None:
        """Lädt Metriken und Resume-State beim Setup."""

        await self._refresh_metrics_from_file()
        await self._refresh_failed_state_counts()
        await self._refresh_resume_state()
        if self.resume_available and self.auto_resume_at is not None:
            self._schedule_auto_resume()

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
                "bypass_skipped": 0,
                "finished_at": None,
                "model": None,
            },
            "totals": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_eur": 0.0,
                "bypass_skipped": 0,
                "runs": 0,
            },
        }

        await self.hass.async_add_executor_job(lambda: self._write_json_file(path, payload))

        self.last_run_total_tokens = 0
        self.last_run_cost_eur = 0.0
        self.last_run_bypass_skipped = 0
        self.total_tokens = 0
        self.total_cost_eur = 0.0
        self.total_bypass_skipped = 0
        self.last_metrics_updated = datetime.now(UTC)
        self.last_status = "metrics_reset"
        self.last_message = "token/cost metrics reset"
        self._notify()

    async def async_export_last_log(self) -> str:
        """Exportiert den letzten kombinierten Log in /config/www für einfachen Download."""

        export_path = Path("/config/www/paperless_kiplus_last_log.txt")
        log_text = self.last_log_combined or "[Kein Log vorhanden]"

        await self.hass.async_add_executor_job(
            lambda: export_path.parent.mkdir(parents=True, exist_ok=True)
        )
        await self.hass.async_add_executor_job(
            lambda: export_path.write_text(log_text, encoding="utf-8")
        )

        self.last_log_export_path = str(export_path)
        self.last_log_export_url = f"/local/paperless_kiplus_last_log.txt?v={int(datetime.now(UTC).timestamp())}"
        self.last_status = "log_exported"
        self.last_message = f"log exported to {self.last_log_export_url}"
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Paperless KIplus Log-Export",
                "message": (
                    "Der Log wurde exportiert.\n\n"
                    f"[Log herunterladen]({self.last_log_export_url})\n\n"
                    f"Pfad: `{self.last_log_export_path}`"
                ),
                "notification_id": "paperless_kiplus_log_export",
            },
            blocking=True,
        )
        self._notify()
        return self.last_log_export_url

    async def _async_show_document_link(
        self,
        *,
        title: str,
        document_id: int | None,
        document_url: str,
        notification_id: str,
    ) -> str:
        """Shows a clickable Paperless document link in a HA notification."""

        if not document_url:
            self.last_status = "document_link_unavailable"
            self.last_message = "no Paperless document link is available yet"
            self._notify()
            return ""

        display_title = title or f"Dokument {document_id}"
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": display_title,
                "message": (
                    f"[Dokument in Paperless öffnen]({document_url})\n\n"
                    f"Dokument-ID: `{document_id}`\n\n"
                    f"URL: `{document_url}`"
                ),
                "notification_id": notification_id,
            },
            blocking=True,
        )
        self.last_status = "document_link_ready"
        self.last_message = f"document link ready for {display_title}"
        self._notify()
        return document_url

    async def async_open_current_document(self) -> str:
        """Shows a clickable link for the document currently being processed."""

        return await self._async_show_document_link(
            title=self.progress_current_document_title,
            document_id=self.progress_current_document_id,
            document_url=self.progress_current_document_url,
            notification_id="paperless_kiplus_current_document_link",
        )

    async def async_open_last_completed_document(self) -> str:
        """Shows a clickable link for the last completed document."""

        return await self._async_show_document_link(
            title=self.last_completed_document_title,
            document_id=self.last_completed_document_id,
            document_url=self.last_completed_document_url,
            notification_id="paperless_kiplus_last_completed_document_link",
        )

    async def async_open_worker_ui(self) -> str:
        """Zeigt bei Bedarf einen klickbaren Link zur externen Worker-Oberfläche."""

        if not self.worker_ui_url:
            self.last_status = "worker_ui_unavailable"
            self.last_message = (
                "keine externe Worker-Weboberfläche konfiguriert; "
                "lokaler Home-Assistant-Modus ist aktiv"
            )
            self._notify()
            return ""

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Paperless KIplus Worker-Weboberfläche",
                "message": (
                    f"[Worker-Weboberfläche öffnen]({self.worker_ui_url})\n\n"
                    f"URL: `{self.worker_ui_url}`"
                ),
                "notification_id": "paperless_kiplus_worker_ui_link",
            },
            blocking=True,
        )
        self.last_status = "worker_ui_link_ready"
        self.last_message = "worker web interface link prepared"
        self._notify()
        return self.worker_ui_url

    async def async_show_last_log(self) -> None:
        """Zeigt den Inhalt des letzten Protokolls direkt als HA-Benachrichtigung an."""

        log_text = self.last_log_combined or "[Kein Log vorhanden]"
        if len(log_text) > 15000:
            log_text = log_text[:14997] + "..."

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Paperless KIplus Letztes Protokoll",
                "message": log_text,
                "notification_id": "paperless_kiplus_last_log",
            },
            blocking=True,
        )
        self.last_status = "log_shown"
        self.last_message = "last log shown in persistent notification"
        self._notify()

    async def async_reset_failed_documents(self) -> None:
        """Löscht Quarantäne-/Bypass-Dateien, damit Failed-Dokumente neu versucht werden."""

        config_payload: dict = {}
        if self.managed_config_enabled and self.managed_config_yaml.strip():
            try:
                parsed = yaml.safe_load(self.managed_config_yaml) or {}
                if isinstance(parsed, dict):
                    config_payload = parsed
            except yaml.YAMLError:
                config_payload = {}
        else:
            config_path = Path(self.config_file)
            if not config_path.is_absolute():
                config_path = Path(self.workdir) / config_path
            if config_path.exists():
                try:
                    parsed = await self.hass.async_add_executor_job(
                        lambda: yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                    )
                    if isinstance(parsed, dict):
                        config_payload = parsed
                except (OSError, yaml.YAMLError):
                    config_payload = {}

        file_candidates = {
            str(config_payload.get("failed_documents_file", "failed_documents.json")).strip(),
            str(config_payload.get("failed_patch_cache_file", "failed_patch_cache.json")).strip(),
            str(config_payload.get("tag_bypass_file", "tag_bypass_documents.json")).strip(),
        }
        file_candidates = {name for name in file_candidates if name}

        deleted_count = 0
        for name in sorted(file_candidates):
            path = Path(name)
            if not path.is_absolute():
                path = Path(self.workdir) / path
            if path.exists():
                try:
                    await self.hass.async_add_executor_job(path.unlink)
                    deleted_count += 1
                except OSError as exc:
                    _LOGGER.warning("Konnte Failed-Datei nicht löschen (%s): %s", path, exc)

        self.last_status = "failed_docs_reset"
        self.last_message = f"failed/quarantine documents reset ({deleted_count} files)"
        await self._refresh_failed_state_counts()
        self._notify()

    async def async_export_worker_config(
        self,
        *,
        remote_upload: bool = False,
        announce: bool = True,
    ) -> str:
        """Exportiert die effektive YAML für einen externen Worker als Datei.

        Im lokalen Modus existiert kein Remote-Ziel. Wir erzeugen deshalb nur
        eine herunterladbare Exportdatei, damit dieselbe Button-/Service-API wie
        im Remote-Modus genutzt werden kann.
        """

        del remote_upload
        export_path = Path("/config/www/paperless_kiplus_worker_config.yaml")
        yaml_text = build_effective_managed_config_yaml(
            self.managed_config_yaml,
            input_cost_per_1k_tokens_eur=self.input_cost_per_1k_tokens_eur,
            output_cost_per_1k_tokens_eur=self.output_cost_per_1k_tokens_eur,
            already_classified_skip=self.already_classified_skip,
            already_classified_require_ki_tag=self.already_classified_require_ki_tag,
            precheck_min_content_chars=self.precheck_min_content_chars,
            precheck_min_word_count=self.precheck_min_word_count,
            precheck_min_alnum_ratio=self.precheck_min_alnum_ratio,
            precheck_blocked_filename_patterns=self.precheck_blocked_filename_patterns,
            precheck_image_only_gate=self.precheck_image_only_gate,
            precheck_duplicate_hash_gate=self.precheck_duplicate_hash_gate,
            precheck_duplicate_apply_metadata=self.precheck_duplicate_apply_metadata,
            reprocess_ki_tagged_documents=self.reprocess_ki_tagged_documents,
            enable_parallel_ai=self.enable_parallel_ai,
            max_parallel_ai_jobs=self.max_parallel_ai_jobs,
            enable_tax_enrichment=self.enable_tax_enrichment,
            tax_process_ki_tagged_documents=self.tax_process_ki_tagged_documents,
            tax_personal_context=self.tax_personal_context,
        )
        await self.hass.async_add_executor_job(
            lambda: export_path.parent.mkdir(parents=True, exist_ok=True)
        )
        await self.hass.async_add_executor_job(
            lambda: export_path.write_text(yaml_text, encoding="utf-8")
        )
        self.last_config_sync_at = datetime.now(UTC)
        self.last_config_sync_status = "local_export_only"
        self.last_status = "config_exported"
        self.last_message = "worker configuration exported locally"
        if announce:
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Paperless KIplus Worker-Konfiguration",
                    "message": (
                        "Die effektive Worker-Konfiguration wurde exportiert.\n\n"
                        "[Exportdatei herunterladen](/local/paperless_kiplus_worker_config.yaml)"
                    ),
                    "notification_id": "paperless_kiplus_worker_config_export",
                },
                blocking=True,
            )
        self._notify()
        return yaml_text

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
        """Write integration-managed YAML config to disk before script execution."""

        if not self.managed_config_yaml.strip():
            raise ValueError(
                "managed_config_enabled ist aktiv, aber managed_config_yaml ist leer."
            )

        path = Path(config_file)
        if not path.is_absolute():
            path = Path(self.workdir) / path
        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            content = build_effective_managed_config_yaml(
                self.managed_config_yaml,
                input_cost_per_1k_tokens_eur=self.input_cost_per_1k_tokens_eur,
                output_cost_per_1k_tokens_eur=self.output_cost_per_1k_tokens_eur,
                already_classified_skip=self.already_classified_skip,
                already_classified_require_ki_tag=self.already_classified_require_ki_tag,
                precheck_min_content_chars=self.precheck_min_content_chars,
                precheck_min_word_count=self.precheck_min_word_count,
                precheck_min_alnum_ratio=self.precheck_min_alnum_ratio,
                precheck_blocked_filename_patterns=self.precheck_blocked_filename_patterns,
                precheck_image_only_gate=self.precheck_image_only_gate,
                precheck_duplicate_hash_gate=self.precheck_duplicate_hash_gate,
                precheck_duplicate_apply_metadata=self.precheck_duplicate_apply_metadata,
                reprocess_ki_tagged_documents=self.reprocess_ki_tagged_documents,
                enable_parallel_ai=self.enable_parallel_ai,
                max_parallel_ai_jobs=self.max_parallel_ai_jobs,
                enable_tax_enrichment=self.enable_tax_enrichment,
                tax_process_ki_tagged_documents=self.tax_process_ki_tagged_documents,
                tax_personal_context=self.tax_personal_context,
            )
            path.write_text(content, encoding="utf-8")

        await self.hass.async_add_executor_job(_write)

    def _build_command(
        self,
        *,
        config_file: str,
        dry_run: bool,
        all_documents: bool,
        max_documents: int,
        backfill_existing_documents: bool,
        resume_run: bool,
    ) -> list[str]:
        """Build a robust CLI command based on HA options and per-run overrides."""

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
        if backfill_existing_documents and not _has_flag(["--backfill-existing-documents"]):
            args.append("--backfill-existing-documents")
        if max_documents > 0 and not _has_flag(["--max-documents"]):
            args.extend(["--max-documents", str(max_documents)])
        if resume_run and not _has_flag(["--resume-run"]):
            args.append("--resume-run")
        if not _has_flag(["--run-state-file"]):
            args.extend(["--run-state-file", str(self._run_state_path())])
        if not _has_flag(["--stop-request-file"]):
            args.extend(["--stop-request-file", str(self._stop_request_path())])

        return args

    def _schedule_auto_resume(self) -> None:
        """Schedules an automatic resume if a provider backoff window exists."""

        if self.auto_resume_at is None:
            return
        self._cancel_auto_resume_task()
        self._auto_resume_task = self.hass.async_create_task(self._auto_resume_worker())

    async def _auto_resume_worker(self) -> None:
        """Waits until auto_resume_at and then resumes the paused run."""

        if self.auto_resume_at is None:
            return
        delay = max(0.0, (self.auto_resume_at - datetime.now(UTC)).total_seconds())
        try:
            await asyncio.sleep(delay)
            if self.running or not self._run_state_path().exists():
                return
            self.last_status = "auto_resuming"
            self.last_message = "auto resume after provider backoff"
            self._notify()
            await self.async_resume(force=True)
        except asyncio.CancelledError:
            return

    def _cancel_auto_resume_task(self) -> None:
        """Cancels a pending automatic resume task if one exists."""

        if self._auto_resume_task is None:
            return
        if not self._auto_resume_task.done():
            self._auto_resume_task.cancel()
        self._auto_resume_task = None

    async def _force_stop_after_grace(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        """Eskaliert von terminate auf kill, falls der Prozess nicht endet."""

        try:
            await asyncio.sleep(FORCE_STOP_GRACE_SECONDS)
            if process.returncode is not None:
                return
            self.last_message = (
                "immediate stop still pending; process did not exit after terminate and will be killed"
            )
            self._notify()
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        except asyncio.CancelledError:
            return

    def _cancel_force_stop_task(self) -> None:
        """Cancels the escalation task for hard stops if it is still pending."""

        if self._force_stop_task is None:
            return
        if not self._force_stop_task.done():
            self._force_stop_task.cancel()
        self._force_stop_task = None

    def _append_tail(self, existing: str, line: str) -> str:
        """Appends one line to a rolling output tail."""

        combined = f"{existing}\n{line}".strip() if existing else line
        return combined[-TAIL_LIMIT_CHARS:]

    def _rebuild_combined_log(self) -> None:
        """Rebuilds the combined log payload shown in HA entities."""

        self.last_log_combined = (
            f"[STDOUT]\n{self.last_stdout_tail.strip()}\n\n[STDERR]\n{self.last_stderr_tail.strip()}"
        ).strip()

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        """Converts values to int where possible."""

        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        """Converts values to float where possible."""

        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        """Parses ISO timestamps to aware UTC datetimes where possible."""

        if value in (None, ""):
            return None
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
        """Writes a small runtime JSON file safely."""

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _delete_file(path: Path) -> None:
        """Deletes a runtime helper file if it exists."""

        with contextlib.suppress(OSError):
            if path.exists():
                path.unlink()

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        """Reads a small runtime JSON file defensively for resume preservation."""

        try:
            if not path.exists():
                return {}
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _clear_restart_state_files(self) -> None:
        """Deletes pause/resume markers so the next run starts truly fresh."""

        self._delete_file(self._run_state_path())
        self._delete_file(self._stop_request_path())

    def _persist_force_stop_resume_state(self) -> bool:
        """Preserves the latest known progress as resumable state before a hard stop."""

        base_payload = self._read_json_file(self._run_state_path())
        if not base_payload:
            base_payload = dict(self._latest_runtime_state_payload)
        pause_payload = build_force_stop_resume_payload(base_payload)
        if not pause_payload:
            return False
        self._write_json_file(self._run_state_path(), pause_payload)
        self._latest_runtime_state_payload = dict(pause_payload)
        return True
