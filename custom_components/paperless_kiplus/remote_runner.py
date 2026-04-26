"""Remote worker runner for Paperless KIplus.

Purpose:
- Steuert einen externen Paperless-KIplus-Worker per HTTP statt lokalem
  Subprozess.
- Hält bewusst dieselben Status- und Komfortattribute wie der lokale Runner
  bereit, damit Sensoren, Buttons und Services in Home Assistant weiter
  funktionieren.

Input / Output:
- Input: Integrationsoptionen, Startparameter und Statusantworten des
  Remote-Workers.
- Output: aktualisierte HA-Entitätszustände, Config-Sync zum Worker und
  Benutzeraktionen wie Run/Stop/Resume/Log-Export.

Important invariants:
- Der Worker ist die ausführungstechnische Quelle für Status, Logs und
  Fortschritt im Remote-Modus.
- Vor produktiven Remote-Läufen wird die effektive managed YAML standardmäßig
  zum Worker synchronisiert.
- Der Remote-Modus darf bestehende lokale HA-Dashboards nicht brechen; deshalb
  bleiben die wichtigsten Runner-Attribute kompatibel.

How to debug:
- Prüfe `remote_worker_url`, `last_config_sync_status`, `last_message` und
  `stderr_tail` im Statussensor.
- Die HTTP-Fehler des Workers werden absichtlich nicht verschluckt, sondern als
  konkrete Statusmeldung in Home Assistant gespiegelt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .config_export import build_effective_managed_config_yaml
from .const import SIGNAL_STATUS_UPDATED

_LOGGER = logging.getLogger(__name__)

REMOTE_POLL_INTERVAL_SECONDS = 5


@dataclass
class RunResult:
    """Result metadata for a worker command."""

    status: str
    exit_code: int | None
    message: str


class RemotePaperlessRunner:
    """Home Assistant side adapter for the standalone remote worker."""

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
        remote_worker_url: str,
        remote_worker_token: str,
        remote_worker_verify_ssl: bool,
        remote_worker_sync_config: bool,
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

        self.execution_mode = "remote_worker"
        self.remote_worker_url = str(remote_worker_url or "").strip().rstrip("/")
        self.remote_worker_token = str(remote_worker_token or "").strip()
        self.remote_worker_verify_ssl = bool(remote_worker_verify_ssl)
        self.remote_worker_sync_config = bool(remote_worker_sync_config)
        self.worker_ui_url = self.remote_worker_url
        self.last_config_sync_at: datetime | None = None
        self.last_config_sync_status: str = "idle"

        self._poll_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._session = async_get_clientsession(hass)

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
        self.progress_current_document_url: str = ""
        self.progress_budget_used: int = 0
        self.progress_pending_documents: int = 0
        self.progress_last_event_at: datetime | None = None
        self.paperless_base_url: str = ""
        self.last_completed_document_id: int | None = None
        self.last_completed_document_title: str = ""
        self.last_completed_document_url: str = ""
        self.last_completed_document_at: datetime | None = None
        self._run_state_path_text: str = ""
        self._stop_request_path_text: str = ""
        self._worker_last_payload: dict[str, Any] = {}

    @property
    def cooldown_until(self) -> datetime | None:
        """Remote worker handles backoff itself; HA does not add extra cooldown."""

        return None

    @property
    def run_state_path(self) -> str:
        return self._run_state_path_text

    @property
    def stop_request_path(self) -> str:
        return self._stop_request_path_text

    def _notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATUS_UPDATED)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.remote_worker_token:
            headers["Authorization"] = f"Bearer {self.remote_worker_token}"
        return headers

    def _worker_url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        return f"{self.remote_worker_url}{normalized_path}"

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def _api_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Executes a JSON API request against the remote worker."""

        if not self.remote_worker_url:
            raise ValueError("remote_worker_url ist nicht konfiguriert.")

        timeout = aiohttp.ClientTimeout(total=60)
        async with self._session.request(
            method,
            self._worker_url(path),
            headers=self._headers(),
            json=payload,
            ssl=self.remote_worker_verify_ssl,
            timeout=timeout,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Remote-Worker API Fehler {response.status} für {path}: "
                    f"{text[:500] or response.reason}"
                )
            if not text.strip():
                return {}
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise RuntimeError(f"Remote-Worker API Antwort für {path} ist kein JSON-Objekt.")
            return parsed

    async def _api_text(self, path: str) -> str:
        """Loads plain-text responses such as log downloads from the worker."""

        if not self.remote_worker_url:
            raise ValueError("remote_worker_url ist nicht konfiguriert.")
        timeout = aiohttp.ClientTimeout(total=120)
        async with self._session.get(
            self._worker_url(path),
            headers=self._headers(),
            ssl=self.remote_worker_verify_ssl,
            timeout=timeout,
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    f"Remote-Worker API Fehler {response.status} für {path}: "
                    f"{text[:500] or response.reason}"
                )
            return text

    def _apply_status_payload(self, payload: dict[str, Any]) -> None:
        """Maps a worker status payload onto the HA runner compatibility fields."""

        if not isinstance(payload, dict):
            return
        self._worker_last_payload = dict(payload)

        self.last_status = str(payload.get("status") or self.last_status or "idle")
        self.last_message = str(payload.get("message") or self.last_message or "")
        self.running = bool(payload.get("running", False))
        self.last_exit_code = payload.get("last_exit_code")
        self.last_started = self._parse_datetime(payload.get("last_started"))
        self.last_finished = self._parse_datetime(payload.get("last_finished"))
        self.last_command_executed = str(payload.get("last_command_executed") or "")
        self.last_stdout_tail = str(payload.get("stdout_tail") or "")
        self.last_stderr_tail = str(payload.get("stderr_tail") or "")
        self.last_summary_line = str(payload.get("summary_line") or "")
        self.last_cost_line = str(payload.get("cost_line") or "")
        self.last_log_combined = str(payload.get("log_text") or payload.get("last_log_combined") or "")
        self.last_scanned = self._safe_int(payload.get("last_scanned"))
        self.last_updated = self._safe_int(payload.get("last_updated"))
        self.last_skipped = self._safe_int(payload.get("last_skipped"))
        self.last_failed = self._safe_int(payload.get("last_failed"))
        self.last_run_total_tokens = self._safe_int(payload.get("last_run_total_tokens"))
        self.last_run_cost_eur = self._safe_float(payload.get("last_run_cost_eur"))
        self.last_run_bypass_skipped = self._safe_int(payload.get("last_run_bypass_skipped"))
        self.total_tokens = self._safe_int(payload.get("total_tokens"))
        self.total_cost_eur = self._safe_float(payload.get("total_cost_eur"))
        self.total_bypass_skipped = self._safe_int(payload.get("total_bypass_skipped"))
        self.active_quarantine_count = self._safe_int(payload.get("active_quarantine_count"))
        self.active_bypass_count = self._safe_int(payload.get("active_bypass_count"))
        self.resume_available = bool(payload.get("resume_available", False))
        self.pause_reason = str(payload.get("pause_reason") or "")
        self.auto_resume_at = self._parse_datetime(payload.get("auto_resume_at"))
        self.stop_requested = bool(payload.get("stop_requested", False))
        self.force_stop_requested = bool(payload.get("force_stop_requested", False))
        self.progress_total_documents = self._safe_int(payload.get("progress_total_documents"))
        self.progress_completed_documents = self._safe_int(payload.get("progress_completed_documents"))
        self.progress_percent = self._safe_float(payload.get("progress_percent"))
        self.progress_scanned = self._safe_int(payload.get("progress_scanned"))
        self.progress_updated = self._safe_int(payload.get("progress_updated"))
        self.progress_skipped = self._safe_int(payload.get("progress_skipped"))
        self.progress_failed = self._safe_int(payload.get("progress_failed"))
        self.progress_bypassed = self._safe_int(payload.get("progress_bypassed"))
        self.progress_bypass_skipped = self._safe_int(payload.get("progress_bypass_skipped"))
        self.progress_prefiltered_ki_tagged = self._safe_int(
            payload.get("progress_prefiltered_ki_tagged")
        )
        self.progress_budget_used = self._safe_int(payload.get("progress_budget_used"))
        self.progress_pending_documents = self._safe_int(payload.get("progress_pending_documents"))
        self.progress_current_document_id = payload.get("progress_current_document_id")
        self.progress_current_document_title = str(payload.get("progress_current_document_title") or "")
        self.progress_current_document_url = str(payload.get("progress_current_document_url") or "")
        self.last_completed_document_id = payload.get("last_completed_document_id")
        self.last_completed_document_title = str(payload.get("last_completed_document_title") or "")
        self.last_completed_document_url = str(payload.get("last_completed_document_url") or "")
        self.last_completed_document_at = self._parse_datetime(payload.get("last_completed_document_at"))
        self.progress_last_event_at = self._parse_datetime(payload.get("progress_last_event_at"))
        self.paperless_base_url = str(payload.get("paperless_base_url") or self.paperless_base_url or "")
        self._run_state_path_text = str(payload.get("run_state_file") or "")
        self._stop_request_path_text = str(payload.get("stop_request_file") or "")
        self.last_config_sync_status = str(payload.get("last_config_sync_status") or self.last_config_sync_status)
        self.last_config_sync_at = self._parse_datetime(payload.get("last_config_sync_at")) or self.last_config_sync_at
        self.last_log_export_path = str(payload.get("last_log_export_path") or self.last_log_export_path or "")
        worker_export_url = str(payload.get("last_log_export_url") or "").strip()
        if worker_export_url:
            if worker_export_url.startswith("http://") or worker_export_url.startswith("https://"):
                self.last_log_export_url = worker_export_url
            else:
                self.last_log_export_url = self._worker_url(worker_export_url)

    async def _refresh_status(self) -> None:
        payload = await self._api_json("GET", "/api/status")
        self._apply_status_payload(payload)
        self._notify()

    async def _poll_loop(self) -> None:
        """Refresh worker status periodically while runs are active or resumable."""

        try:
            while True:
                await self._refresh_status()
                if not self.running and not self.resume_available and self.last_status not in {
                    "waiting_auto_resume",
                    "paused",
                }:
                    break
                await asyncio.sleep(REMOTE_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.last_status = "remote_poll_error"
            self.last_message = str(exc)
            self.last_stderr_tail = str(exc)
            self._notify()

    def _ensure_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            return
        self._poll_task = self.hass.async_create_task(self._poll_loop())

    def _cancel_polling(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    def _effective_config_yaml(self) -> str:
        """Builds the exact YAML that the remote worker should execute."""

        if not self.managed_config_enabled or not str(self.managed_config_yaml or "").strip():
            config_path = Path(self.config_file)
            if config_path.exists():
                return config_path.read_text(encoding="utf-8")
            raise ValueError("Keine managed YAML verfügbar und config_file nicht lesbar.")
        return build_effective_managed_config_yaml(
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

    async def async_export_worker_config(
        self,
        *,
        remote_upload: bool = True,
        announce: bool = True,
    ) -> str:
        """Exports the effective config locally and optionally uploads it to the worker."""

        yaml_text = await self.hass.async_add_executor_job(self._effective_config_yaml)
        export_path = Path("/config/www/paperless_kiplus_worker_config.yaml")
        await self.hass.async_add_executor_job(
            lambda: export_path.parent.mkdir(parents=True, exist_ok=True)
        )
        await self.hass.async_add_executor_job(
            lambda: export_path.write_text(yaml_text, encoding="utf-8")
        )

        upload_message = "lokal exportiert"
        if remote_upload:
            response = await self._api_json(
                "POST",
                "/api/config/import",
                payload={
                    "yaml_text": yaml_text,
                    "source": "home_assistant",
                },
            )
            upload_message = str(response.get("message") or "zum Worker übertragen")

        self.last_config_sync_at = datetime.now(UTC)
        self.last_config_sync_status = "success"
        self.last_status = "config_exported"
        self.last_message = f"Worker-Konfiguration {upload_message}"
        if announce:
            local_url = "/local/paperless_kiplus_worker_config.yaml"
            worker_config_url = ""
            if self.remote_worker_url:
                worker_config_url = self._worker_url("/api/config/download")
            message = (
                "Die effektive Worker-Konfiguration wurde exportiert.\n\n"
                f"[Lokale Exportdatei öffnen]({local_url})"
            )
            if worker_config_url:
                message += f"\n\n[Worker-Konfiguration herunterladen]({worker_config_url})"
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "Paperless KIplus Worker-Konfiguration",
                    "message": message,
                    "notification_id": "paperless_kiplus_worker_config_export",
                },
                blocking=True,
            )
        self._notify()
        return yaml_text

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
        """Starts a remote run after syncing the effective config if requested."""

        del config_file  # Remote worker owns its own config file path.
        async with self._lock:
            if self.remote_worker_sync_config and self.managed_config_enabled:
                await self.async_export_worker_config(remote_upload=True, announce=False)

            payload = await self._api_json(
                "POST",
                "/api/run" if not resume_run else "/api/resume",
                payload={
                    "force": force,
                    "dry_run": self.default_dry_run if dry_run is None else bool(dry_run),
                    "all_documents": self.default_all_documents if all_documents is None else bool(all_documents),
                    "max_documents": self.default_max_documents if max_documents is None else int(max_documents),
                    "backfill_existing_documents": bool(backfill_existing_documents),
                },
            )
            self._apply_status_payload(payload.get("status") or payload)
            if self.running or self.last_status in {"waiting_auto_resume", "paused"}:
                self._ensure_polling()
            self._notify()
            return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_request_stop(self) -> RunResult:
        payload = await self._api_json("POST", "/api/stop", payload={})
        self._apply_status_payload(payload.get("status") or payload)
        self._ensure_polling()
        self._notify()
        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_force_stop(self) -> RunResult:
        payload = await self._api_json("POST", "/api/stop_now", payload={})
        self._apply_status_payload(payload.get("status") or payload)
        self._ensure_polling()
        self._notify()
        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_resume(self, *, force: bool = True) -> RunResult:
        payload = await self._api_json("POST", "/api/resume", payload={"force": force})
        self._apply_status_payload(payload.get("status") or payload)
        self._ensure_polling()
        self._notify()
        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_restart(
        self,
        *,
        force: bool = True,
        backfill_existing_documents: bool | None = None,
    ) -> RunResult:
        if self.remote_worker_sync_config and self.managed_config_enabled:
            await self.async_export_worker_config(remote_upload=True, announce=False)
        payload = await self._api_json(
            "POST",
            "/api/restart",
            payload={
                "force": force,
                "backfill_existing_documents": backfill_existing_documents,
            },
        )
        self._apply_status_payload(payload.get("status") or payload)
        self._ensure_polling()
        self._notify()
        return RunResult(self.last_status, self.last_exit_code, self.last_message)

    async def async_reset_metrics(self) -> None:
        payload = await self._api_json("POST", "/api/metrics/reset", payload={})
        self._apply_status_payload(payload.get("status") or payload)
        self._notify()

    async def async_reset_failed_documents(self) -> None:
        payload = await self._api_json("POST", "/api/failed/reset", payload={})
        self._apply_status_payload(payload.get("status") or payload)
        self._notify()

    async def async_export_last_log(self) -> str:
        """Downloads the worker log and re-exports it into HA's `/config/www`."""

        log_text = await self._api_text("/api/logs/download")
        export_path = Path("/config/www/paperless_kiplus_last_log.txt")
        await self.hass.async_add_executor_job(
            lambda: export_path.parent.mkdir(parents=True, exist_ok=True)
        )
        await self.hass.async_add_executor_job(
            lambda: export_path.write_text(log_text, encoding="utf-8")
        )
        self.last_log_combined = log_text
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
                    "Der Worker-Log wurde exportiert.\n\n"
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
        return await self._async_show_document_link(
            title=self.progress_current_document_title,
            document_id=self.progress_current_document_id,
            document_url=self.progress_current_document_url,
            notification_id="paperless_kiplus_current_document_link",
        )

    async def async_open_last_completed_document(self) -> str:
        return await self._async_show_document_link(
            title=self.last_completed_document_title,
            document_id=self.last_completed_document_id,
            document_url=self.last_completed_document_url,
            notification_id="paperless_kiplus_last_completed_document_link",
        )

    async def async_open_worker_ui(self) -> str:
        if not self.worker_ui_url:
            self.last_status = "worker_ui_unavailable"
            self.last_message = "remote_worker_url ist nicht gesetzt."
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
        log_text = self.last_log_combined or await self._api_text("/api/logs/download")
        if len(log_text) > 15000:
            log_text = log_text[:14997] + "..."
        self.last_log_combined = log_text
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
        self.last_message = "last remote worker log shown in persistent notification"
        self._notify()

    async def async_load_initial_metrics(self) -> None:
        await self._refresh_status()
        if self.running or self.resume_available or self.last_status == "waiting_auto_resume":
            self._ensure_polling()

    async def async_shutdown(self) -> None:
        self._cancel_polling()
