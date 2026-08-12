"""Persist Cloudflare-safe background-job metadata in SQLite.

Purpose:
- Admit potentially long HTTP operations quickly and run them outside the
  request thread.
- Keep job/request IDs, exact progress, terminal results, and safe errors
  available across browser reloads and worker restarts.

Input / Output:
- Input: validated operation names, minimal non-secret parameters, optional
  idempotency keys, and bounded Python runner callbacks.
- Output: public job dictionaries that never expose callbacks, secret values,
  raw exceptions, database paths, or idempotency hashes.

Important invariants:
- SQLite transactions serialize active resource ownership across HTTP threads.
- Mutating jobs are never retried automatically after process interruption.
- Only explicitly allowlisted read-only jobs may be resubmitted on startup.
- Terminal rows expire after 24 hours and are capped at 200 records.

How to debug:
- Set ``LOG_LEVEL=DEBUG`` and search logs for ``job_id`` plus ``request_id``.
- Inspect ``state/background_jobs.sqlite3`` locally with SQLite tooling; never
  copy raw job databases into issues or chat messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("paperless_worker.jobs")
TERMINAL_STATUSES = {"succeeded", "failed", "interrupted", "cancelled"}
ACTIVE_STATUSES = {"queued", "running"}
RETENTION_HOURS = 24
MAX_TERMINAL_JOBS = 200
MAX_IDEMPOTENCY_KEY_CHARS = 200

ProgressCallback = Callable[[dict[str, Any]], None]
JobRunner = Callable[[ProgressCallback], dict[str, Any]]


class JobConflictError(RuntimeError):
    """Signal that one serialized resource already has an active owner."""

    def __init__(self, existing_job: dict[str, Any]) -> None:
        super().__init__("Für diese Ressource läuft bereits ein Hintergrundjob.")
        self.existing_job = existing_job


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_user_error(exc: Exception) -> tuple[str, str]:
    """Map exceptions to bounded messages without echoing provider details."""

    if isinstance(exc, ValueError):
        return (
            "invalid_input",
            (
                "Die Job-Ausführung wurde wegen ungültiger Eingaben abgebrochen. "
                "Mit request_id in den redigierten Worker-Logs nachsehen."
            ),
        )
    return (
        "operation_failed",
        "Der Hintergrundjob ist fehlgeschlagen. Mit request_id in den redigierten Worker-Logs nachsehen.",
    )


class PersistentJobStore:
    """Thread-safe, process-persistent job admission and execution store."""

    def __init__(self, database_path: Path, *, max_workers: int = 3) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(int(max_workers), 8)),
            thread_name_prefix="paperless-job",
        )
        self._initialize_schema()
        try:
            self.database_path.chmod(0o600)
        except OSError:
            LOGGER.warning("Job-Datenbankrechte konnten nicht auf 0600 gesetzt werden.")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one short-lived connection and always close its file handle."""

        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_schema(self) -> None:
        # Why this exists: CREATE TABLE IF NOT EXISTS is an additive migration
        # that leaves existing worker state and sorter files untouched.
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS background_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    operation TEXT NOT NULL,
                    resource_key TEXT,
                    idempotency_hash TEXT,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    progress_json TEXT,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_background_jobs_idempotency
                    ON background_jobs(operation, idempotency_hash)
                    WHERE idempotency_hash IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS uq_background_jobs_active_resource
                    ON background_jobs(resource_key)
                    WHERE resource_key IS NOT NULL
                      AND status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS ix_background_jobs_updated_at
                    ON background_jobs(updated_at);
                """
            )

    @staticmethod
    def _idempotency_hash(operation: str, idempotency_key: str | None) -> str | None:
        raw = str(idempotency_key or "").strip()
        if not raw:
            return None
        if len(raw) > MAX_IDEMPOTENCY_KEY_CHARS:
            raise ValueError(
                f"Idempotency-Key darf höchstens {MAX_IDEMPOTENCY_KEY_CHARS} Zeichen lang sein."
            )
        return hashlib.sha256(f"{operation}\0{raw}".encode()).hexdigest()

    def _cleanup(self, connection: sqlite3.Connection) -> None:
        cutoff = (datetime.now(UTC) - timedelta(hours=RETENTION_HOURS)).isoformat()
        connection.execute(
            "DELETE FROM background_jobs WHERE status IN ('succeeded', 'failed', 'interrupted', 'cancelled') AND updated_at < ?",
            (cutoff,),
        )
        connection.execute(
            """
            DELETE FROM background_jobs
            WHERE job_id IN (
                SELECT job_id FROM background_jobs
                WHERE status IN ('succeeded', 'failed', 'interrupted', 'cancelled')
                ORDER BY updated_at DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (MAX_TERMINAL_JOBS,),
        )

    def submit(
        self,
        *,
        operation: str,
        params: dict[str, Any],
        resource_key: str | None,
        idempotency_key: str | None,
        runner: JobRunner,
        replace_active_operations: set[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically admit one job and start it in the bounded executor.

        Example: two simultaneous ``sorter_run`` requests receive one active
        owner; a repeated request with the same idempotency key receives the
        original job with ``deduplicated=True``.
        """

        normalized_operation = re.sub(r"[^a-z0-9_]+", "_", str(operation).lower()).strip("_")
        if not normalized_operation:
            raise ValueError("Hintergrundjob benötigt einen gültigen Operationstyp.")
        idempotency_hash = self._idempotency_hash(normalized_operation, idempotency_key)
        job_id = f"job_{uuid.uuid4().hex}"
        request_id = f"req_{uuid.uuid4().hex}"
        now = _utc_now()
        params_text = json.dumps(params, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup(connection)
                if idempotency_hash:
                    existing = connection.execute(
                        "SELECT * FROM background_jobs WHERE operation = ? AND idempotency_hash = ?",
                        (normalized_operation, idempotency_hash),
                    ).fetchone()
                    if existing is not None:
                        connection.commit()
                        return self._public_row(existing), True
                if resource_key:
                    active = connection.execute(
                        "SELECT * FROM background_jobs WHERE resource_key = ? AND status IN ('queued', 'running') ORDER BY created_at LIMIT 1",
                        (resource_key,),
                    ).fetchone()
                    if active is not None:
                        replaceable = replace_active_operations or set()
                        if str(active["operation"]) not in replaceable:
                            connection.commit()
                            raise JobConflictError(self._public_row(active))
                        # Why this exists: a restart must be able to supersede
                        # an active sorter run without opening a second write
                        # lane. The old callback may finish later, but all of
                        # its terminal UPDATEs require status='running' and can
                        # therefore never overwrite this cancellation.
                        connection.execute(
                            """
                            UPDATE background_jobs
                            SET status = 'cancelled', phase = 'superseded',
                                error_code = 'superseded_by_restart',
                                error_message = 'Der Lauf wurde durch einen kontrollierten Restart ersetzt.',
                                finished_at = ?, updated_at = ?
                            WHERE job_id = ? AND status IN ('queued', 'running')
                            """,
                            (now, now, active["job_id"]),
                        )
                connection.execute(
                    """
                    INSERT INTO background_jobs (
                        job_id, request_id, operation, resource_key,
                        idempotency_hash, status, phase, params_json,
                        attempt_count, max_attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', 'queued', ?, 0, 1, ?, ?)
                    """,
                    (
                        job_id,
                        request_id,
                        normalized_operation,
                        resource_key,
                        idempotency_hash,
                        params_text,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        self._executor.submit(self._execute, job_id, request_id, runner)
        job = self.get(job_id)
        if job is None:  # pragma: no cover - defensive database invariant
            raise RuntimeError("Der eben angelegte Hintergrundjob ist nicht lesbar.")
        return job, False

    def close(self, *, wait: bool = True) -> None:
        """Stop accepting work and optionally wait for active callbacks."""

        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _execute(self, job_id: str, request_id: str, runner: JobRunner) -> None:
        now = _utc_now()
        with self._connection() as connection:
            changed = connection.execute(
                """
                UPDATE background_jobs
                SET status = 'running', phase = 'running', attempt_count = attempt_count + 1,
                    started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (now, now, job_id),
            ).rowcount
        if changed != 1:
            return

        def progress_callback(progress: dict[str, Any]) -> None:
            self.update_progress(job_id, progress)

        try:
            result = runner(progress_callback)
            safe_result = result if isinstance(result, dict) else {"ok": True}
            finished_at = _utc_now()
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'succeeded', phase = 'completed', result_json = ?,
                        finished_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (
                        json.dumps(safe_result, ensure_ascii=False, separators=(",", ":")),
                        finished_at,
                        finished_at,
                        job_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            error_code, error_message = _safe_user_error(exc)
            finished_at = _utc_now()
            LOGGER.error(
                "background_job_failed job_id=%s request_id=%s operation_error=%s",
                job_id,
                request_id,
                type(exc).__name__,
            )
            with self._connection() as connection:
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'failed', phase = 'failed', error_code = ?,
                        error_message = ?, finished_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (error_code, error_message, finished_at, finished_at, job_id),
                )

    def update_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        """Persist exact counters only; callers use ``None`` when ETA is unknown."""

        allowed = {
            "phase",
            "progress_percent",
            "estimated_seconds_remaining",
            "total",
            "completed",
            "scanned",
            "updated",
            "skipped",
            "failed",
        }
        safe_progress = {key: progress.get(key) for key in allowed if key in progress}
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                "UPDATE background_jobs SET progress_json = ?, phase = ?, updated_at = ? WHERE job_id = ? AND status = 'running'",
                (
                    json.dumps(safe_progress, ensure_ascii=False, separators=(",", ":")),
                    str(safe_progress.get("phase") or "running")[:80],
                    now,
                    job_id,
                ),
            )

    def reconcile_startup(
        self,
        *,
        read_only_runners: dict[str, Callable[[dict[str, Any]], JobRunner]] | None = None,
    ) -> dict[str, int]:
        """Recover read-only queued work and safely interrupt all mutations."""

        factories = read_only_runners or {}
        resumed = 0
        interrupted = 0
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM background_jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
            for row in rows:
                operation = str(row["operation"])
                if operation in factories:
                    connection.execute(
                        "UPDATE background_jobs SET status = 'queued', phase = 'recovered', updated_at = ? WHERE job_id = ?",
                        (_utc_now(), row["job_id"]),
                    )
                    try:
                        params = json.loads(row["params_json"] or "{}")
                    except json.JSONDecodeError:
                        params = {}
                    self._executor.submit(
                        self._execute,
                        row["job_id"],
                        row["request_id"],
                        factories[operation](params),
                    )
                    resumed += 1
                    continue
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'interrupted', phase = 'interrupted',
                        error_code = 'worker_restarted',
                        error_message = 'Der Worker wurde während des Jobs neu gestartet. Zustand prüfen und die Aktion kontrolliert erneut auslösen.',
                        finished_at = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, now, row["job_id"]),
                )
                interrupted += 1
        return {"resumed_read_only": resumed, "interrupted_mutations": interrupted}

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"job_[0-9a-f]{32}", str(job_id or "")):
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM background_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            cutoff = (datetime.now(UTC) - timedelta(hours=RETENTION_HOURS)).isoformat()
            if (
                row is not None
                and row["status"] in TERMINAL_STATUSES
                and str(row["updated_at"]) < cutoff
            ):
                connection.execute(
                    "DELETE FROM background_jobs WHERE job_id = ?",
                    (job_id,),
                )
                row = None
        return self._public_row(row) if row is not None else None

    @staticmethod
    def _public_row(row: sqlite3.Row) -> dict[str, Any]:
        progress = json.loads(row["progress_json"]) if row["progress_json"] else None
        result = json.loads(row["result_json"]) if row["result_json"] else None
        payload: dict[str, Any] = {
            "job_id": row["job_id"],
            "request_id": row["request_id"],
            "operation": row["operation"],
            "status": row["status"],
            "phase": row["phase"],
            "progress": progress,
            "progress_percent": progress.get("progress_percent") if progress else None,
            "estimated_seconds_remaining": (
                progress.get("estimated_seconds_remaining") if progress else None
            ),
            "attempt_count": row["attempt_count"],
            "max_attempts": row["max_attempts"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
        }
        if row["status"] == "succeeded":
            payload["result"] = result or {}
        if row["status"] in {"failed", "interrupted", "cancelled"}:
            payload["error"] = {
                "code": row["error_code"] or "operation_failed",
                "message": row["error_message"] or "Der Hintergrundjob ist fehlgeschlagen.",
                "request_id": row["request_id"],
            }
        return payload


def admission_payload(job: dict[str, Any], *, deduplicated: bool) -> dict[str, Any]:
    """Build the stable HTTP 202 response shared by all long operations."""

    job_id = str(job["job_id"])
    return {
        "ok": True,
        "status": job["status"],
        "job_id": job_id,
        "request_id": job["request_id"],
        "status_url": f"/api/jobs/{job_id}",
        "deduplicated": bool(deduplicated),
    }
