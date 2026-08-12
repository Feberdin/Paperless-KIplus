"""Tests for the persistent Cloudflare-safe background-job store.

Purpose:
- Prove that admission is idempotent and parallel-safe.
- Protect restart recovery and safe terminal error behavior.

Input / Output:
- Input: temporary SQLite files and deterministic in-process job callbacks.
- Output: assertions against the same public job dictionaries served by HTTP.

Important invariants:
- At most one active job owns a serialized resource.
- Mutations become interrupted after restart; allowlisted reads may resume.
- Secret-like exception text never reaches persisted public errors.

How to debug:
- Run `python3 -m unittest tests.test_background_jobs -v`.
- Add a temporary `print(store.get(job_id))` only in a local checkout; the
  temporary databases are deleted at test completion.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from background_jobs import JobConflictError, PersistentJobStore


def _wait_for_status(
    store: PersistentJobStore,
    job_id: str,
    statuses: set[str],
    *,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Poll a local store with a short deterministic test deadline."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job and job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(f"Job {job_id} erreichte {sorted(statuses)} nicht rechtzeitig.")


class PersistentJobStoreTests(unittest.TestCase):
    """Covers storage, parallel admission, recovery, and redaction."""

    def test_success_progress_and_idempotent_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PersistentJobStore(Path(tmp_dir) / "jobs.sqlite3")
            try:
                def runner(progress):
                    progress(
                        {
                            "phase": "counting",
                            "progress_percent": 50.0,
                            "estimated_seconds_remaining": None,
                            "completed": 1,
                            "total": 2,
                        }
                    )
                    return {"ok": True, "message": "fertig"}

                first, first_deduplicated = store.submit(
                    operation="review_scan",
                    params={"threshold": 0.84},
                    resource_key="review_scan",
                    idempotency_key="same-browser-action",
                    runner=runner,
                )
                terminal = _wait_for_status(store, first["job_id"], {"succeeded"})
                second, second_deduplicated = store.submit(
                    operation="review_scan",
                    params={"threshold": 0.84},
                    resource_key="review_scan",
                    idempotency_key="same-browser-action",
                    runner=runner,
                )

                self.assertFalse(first_deduplicated)
                self.assertTrue(second_deduplicated)
                self.assertEqual(first["job_id"], second["job_id"])
                self.assertEqual(terminal["result"]["message"], "fertig")
                self.assertIsNone(terminal["estimated_seconds_remaining"])
            finally:
                store.close()

    def test_parallel_admission_keeps_one_resource_owner(self) -> None:
        # Five fresh databases catch timing-sensitive regressions instead of
        # proving the unique-index path only once.
        for repetition in range(5):
            with self.subTest(repetition=repetition), tempfile.TemporaryDirectory() as tmp_dir:
                store = PersistentJobStore(Path(tmp_dir) / "jobs.sqlite3")
                release = threading.Event()
                try:
                    def blocking_runner(_progress, event=release):
                        event.wait(5)
                        return {"ok": True}

                    first, _ = store.submit(
                        operation="review_merge",
                        params={"alias_id": 1, "canonical_id": 2},
                        resource_key="paperless_write",
                        idempotency_key="first",
                        runner=blocking_runner,
                    )
                    _wait_for_status(store, first["job_id"], {"running"})

                    conflicts: list[str] = []
                    unexpected: list[Exception] = []

                    def contend(
                        index: int,
                        current_store=store,
                        current_conflicts=conflicts,
                        current_unexpected=unexpected,
                    ) -> None:
                        try:
                            current_store.submit(
                                operation="sorter_run",
                                params={},
                                resource_key="paperless_write",
                                idempotency_key=f"contender-{index}",
                                runner=lambda _progress: {"ok": True},
                            )
                        except JobConflictError as exc:
                            current_conflicts.append(exc.existing_job["job_id"])
                        except Exception as exc:  # noqa: BLE001 - test captures thread failures
                            current_unexpected.append(exc)

                    threads = [
                        threading.Thread(target=contend, args=(index,))
                        for index in range(8)
                    ]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=5)

                    self.assertEqual(unexpected, [])
                    self.assertEqual(conflicts, [first["job_id"]] * 8)
                finally:
                    release.set()
                    store.close()

    def test_restart_supersedes_sorter_but_not_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PersistentJobStore(Path(tmp_dir) / "jobs.sqlite3", max_workers=2)
            release = threading.Event()
            try:
                active, _ = store.submit(
                    operation="sorter_run",
                    params={},
                    resource_key="paperless_write",
                    idempotency_key="active-sorter",
                    runner=lambda _progress: (release.wait(5), {"ok": True})[1],
                )
                _wait_for_status(store, active["job_id"], {"running"})
                replacement, _ = store.submit(
                    operation="sorter_restart",
                    params={},
                    resource_key="paperless_write",
                    idempotency_key="restart",
                    runner=lambda _progress: {"ok": True},
                    replace_active_operations={"sorter_run", "sorter_resume", "sorter_restart"},
                )

                cancelled = _wait_for_status(store, active["job_id"], {"cancelled"})
                _wait_for_status(store, replacement["job_id"], {"succeeded"})
                self.assertEqual(cancelled["error"]["code"], "superseded_by_restart")
            finally:
                release.set()
                store.close()

    def test_restart_marks_mutation_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = Path(tmp_dir) / "jobs.sqlite3"
            first_store = PersistentJobStore(database)
            release = threading.Event()
            second_store: PersistentJobStore | None = None
            try:
                job, _ = first_store.submit(
                    operation="review_merge",
                    params={"alias_id": 1, "canonical_id": 2},
                    resource_key="paperless_write",
                    idempotency_key=None,
                    runner=lambda _progress: (release.wait(5), {"ok": True})[1],
                )
                _wait_for_status(first_store, job["job_id"], {"running"})
                second_store = PersistentJobStore(database)
                recovery = second_store.reconcile_startup()
                interrupted = _wait_for_status(
                    second_store,
                    job["job_id"],
                    {"interrupted"},
                )

                self.assertEqual(recovery["interrupted_mutations"], 1)
                self.assertEqual(interrupted["error"]["code"], "worker_restarted")
                self.assertEqual(interrupted["attempt_count"], 1)
            finally:
                release.set()
                first_store.close()
                if second_store is not None:
                    second_store.close()

    def test_allowlisted_read_is_resumed_once_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            database = Path(tmp_dir) / "jobs.sqlite3"
            first_store = PersistentJobStore(database)
            release = threading.Event()
            second_store: PersistentJobStore | None = None
            try:
                job, _ = first_store.submit(
                    operation="review_scan",
                    params={"threshold": 0.9},
                    resource_key="review_scan",
                    idempotency_key=None,
                    runner=lambda _progress: (release.wait(5), {"old": True})[1],
                )
                _wait_for_status(first_store, job["job_id"], {"running"})
                second_store = PersistentJobStore(database)
                recovery = second_store.reconcile_startup(
                    read_only_runners={
                        "review_scan": lambda params: (
                            lambda _progress: {"threshold": params["threshold"], "resumed": True}
                        )
                    }
                )
                terminal = _wait_for_status(second_store, job["job_id"], {"succeeded"})

                self.assertEqual(recovery["resumed_read_only"], 1)
                self.assertTrue(terminal["result"]["resumed"])
                self.assertEqual(terminal["attempt_count"], 2)
            finally:
                release.set()
                first_store.close()
                if second_store is not None:
                    second_store.close()

    def test_failure_does_not_persist_secret_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PersistentJobStore(Path(tmp_dir) / "jobs.sqlite3")
            try:
                def failing_runner(_progress):
                    raise ValueError("token=do-not-leak provider payload")

                job, _ = store.submit(
                    operation="review_scan",
                    params={},
                    resource_key="review_scan",
                    idempotency_key=None,
                    runner=failing_runner,
                )
                terminal = _wait_for_status(store, job["job_id"], {"failed"})
                rendered = str(terminal).lower()

                self.assertNotIn("do-not-leak", rendered)
                self.assertEqual(terminal["error"]["code"], "invalid_input")
                self.assertEqual(terminal["max_attempts"], 1)
            finally:
                store.close()

    def test_overlong_idempotency_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PersistentJobStore(Path(tmp_dir) / "jobs.sqlite3")
            try:
                with self.assertRaisesRegex(ValueError, "höchstens 200"):
                    store.submit(
                        operation="review_scan",
                        params={},
                        resource_key="review_scan",
                        idempotency_key="x" * 201,
                        runner=lambda _progress: {"ok": True},
                    )
            finally:
                store.close()

    def test_expired_terminal_job_is_no_longer_public(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = PersistentJobStore(Path(tmp_dir) / "jobs.sqlite3")
            try:
                job, _ = store.submit(
                    operation="review_scan",
                    params={},
                    resource_key="review_scan",
                    idempotency_key=None,
                    runner=lambda _progress: {"ok": True},
                )
                _wait_for_status(store, job["job_id"], {"succeeded"})
                with store._connection() as connection:
                    connection.execute(
                        "UPDATE background_jobs SET updated_at = '2000-01-01T00:00:00+00:00' WHERE job_id = ?",
                        (job["job_id"],),
                    )

                self.assertIsNone(store.get(job["job_id"]))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
