"""Tests for runtime progress, pause/resume, and provider backoff helpers.

Purpose:
- Protect the new pause/resume foundation against regressions.
- Ensure 429 responses become resumable pauses instead of hard document errors.
- Verify persisted run-state files can round-trip safely.

How to run:
- `python3 -m unittest discover -s tests`
"""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
import importlib.util
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

if "yaml" not in sys.modules:
    sys.modules["yaml"] = types.SimpleNamespace(safe_load=lambda *_args, **_kwargs: {})

from paperless_ai_sorter import (
    AiClassifier,
    PendingAiDocument,
    RUN_STATE_VERSION,
    extract_retry_after_seconds_from_error,
    finalize_limited_progress_total,
    load_run_state,
    resolve_runtime_path,
    save_run_state,
)
from tax_enrichment import extract_retry_after_seconds as extract_tax_retry_after_seconds


class RuntimeStateTests(unittest.TestCase):
    """Exercises the resumable runtime helpers used by the HA runner."""

    def test_retry_after_seconds_parser_prefers_headers(self) -> None:
        seconds = extract_retry_after_seconds_from_error(
            "Please try again in 2.533s",
            {"retry-after": "7.5"},
        )
        self.assertEqual(seconds, 7.5)

    def test_retry_after_seconds_parser_reads_openai_message(self) -> None:
        seconds = extract_retry_after_seconds_from_error(
            "Rate limit reached. Please try again in 2.533s."
        )
        self.assertAlmostEqual(seconds or 0.0, 2.533, places=3)
        self.assertAlmostEqual(
            extract_tax_retry_after_seconds("Please try again in 4.25s") or 0.0,
            4.25,
            places=2,
        )

    def test_pending_document_state_roundtrip(self) -> None:
        original = PendingAiDocument(
            document={"id": 123, "title": "Rechnung"},
            doc_id=123,
            doc_key="123",
            title="Rechnung",
            doc_tags={1, 5, 9},
            enrichment_only=True,
        )

        restored = PendingAiDocument.from_state_dict(original.to_state_dict())

        self.assertEqual(restored.document["id"], 123)
        self.assertEqual(restored.doc_id, 123)
        self.assertEqual(restored.doc_key, "123")
        self.assertEqual(restored.title, "Rechnung")
        self.assertEqual(restored.doc_tags, {1, 5, 9})
        self.assertTrue(restored.enrichment_only)

    def test_pending_document_progress_payload_stays_small(self) -> None:
        original = PendingAiDocument(
            document={"id": 123, "title": "Rechnung", "content": "sehr lang" * 100},
            doc_id=123,
            doc_key="123",
            title="Rechnung",
            doc_tags={1, 5, 9},
            enrichment_only=True,
        )

        payload = original.to_progress_dict()

        self.assertEqual(payload["doc_id"], 123)
        self.assertEqual(payload["title"], "Rechnung")
        self.assertNotIn("document", payload)
        self.assertEqual(payload["doc_tags"], [1, 5, 9])

    def test_limited_success_progress_uses_actual_completed_total(self) -> None:
        self.assertEqual(
            finalize_limited_progress_total(
                current_total=50,
                target_documents=50,
                budget_used=28,
                pending_count=0,
            ),
            28,
        )

    def test_limited_success_progress_keeps_total_when_limit_is_exhausted(self) -> None:
        self.assertEqual(
            finalize_limited_progress_total(
                current_total=50,
                target_documents=50,
                budget_used=50,
                pending_count=0,
            ),
            50,
        )

    def test_run_state_roundtrip_and_version_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "run_state.json"
            save_run_state(
                state_path,
                {
                    "status": "paused",
                    "progress": {"completed_documents": 12},
                    "completed_document_ids": [1, 2, 3],
                },
            )

            loaded = load_run_state(state_path)
            self.assertEqual(loaded["version"], RUN_STATE_VERSION)
            self.assertEqual(loaded["status"], "paused")
            self.assertEqual(loaded["progress"]["completed_documents"], 12)

            state_path.write_text(
                '{"version":999,"status":"paused","progress":{"completed_documents":99}}',
                encoding="utf-8",
            )
            self.assertEqual(load_run_state(state_path), {})

    def test_resolve_runtime_path_uses_base_dir_for_relative_paths(self) -> None:
        resolved = resolve_runtime_path("state/run.json", Path("/tmp/base"))
        self.assertEqual(resolved, Path("/tmp/base/state/run.json"))
        self.assertEqual(
            resolve_runtime_path("/tmp/absolute.json", Path("/tmp/base")),
            Path("/tmp/absolute.json"),
        )

    def test_http_429_quota_becomes_pause_error(self) -> None:
        response = requests.Response()
        response.status_code = 429
        response._content = (
            b'{"error":{"message":"You exceeded your current quota.","code":"insufficient_quota"}}'
        )
        http_error = requests.HTTPError("HTTP 429", response=response)

        pause_error = AiClassifier._build_pause_error_from_http_error(http_error)

        self.assertIsNotNone(pause_error)
        self.assertEqual(pause_error.pause_reason, "quota_exhausted")
        self.assertGreaterEqual(pause_error.retry_after_seconds or 0.0, 300.0)

    def test_http_429_rate_limit_keeps_retry_after(self) -> None:
        response = requests.Response()
        response.status_code = 429
        response.headers["retry-after"] = "12.5"
        response._content = (
            b'{"error":{"message":"Rate limit reached on tokens per min.","code":"rate_limit_exceeded"}}'
        )
        http_error = requests.HTTPError("HTTP 429", response=response)

        pause_error = AiClassifier._build_pause_error_from_http_error(http_error)

        self.assertIsNotNone(pause_error)
        self.assertEqual(pause_error.pause_reason, "rate_limit_wait")
        self.assertEqual(pause_error.retry_after_seconds, 12.5)


class HomeAssistantRunnerHelperTests(unittest.TestCase):
    """Prüft kleine, reine Helper-Logik des HA-Runners ohne echte HA-Installation."""

    @classmethod
    def setUpClass(cls) -> None:
        homeassistant_module = types.ModuleType("homeassistant")
        core_module = types.ModuleType("homeassistant.core")
        helpers_module = types.ModuleType("homeassistant.helpers")
        dispatcher_module = types.ModuleType("homeassistant.helpers.dispatcher")

        class _FakeHomeAssistant:  # noqa: D401 - schlanker Stub für den Import
            """Minimaler Typstub für den Runner-Import."""

        dispatcher_module.async_dispatcher_send = lambda *_args, **_kwargs: None
        core_module.HomeAssistant = _FakeHomeAssistant
        homeassistant_module.core = core_module
        homeassistant_module.helpers = helpers_module
        helpers_module.dispatcher = dispatcher_module

        sys.modules.setdefault("homeassistant", homeassistant_module)
        sys.modules.setdefault("homeassistant.core", core_module)
        sys.modules.setdefault("homeassistant.helpers", helpers_module)
        sys.modules.setdefault("homeassistant.helpers.dispatcher", dispatcher_module)

        custom_components_module = types.ModuleType("custom_components")
        package_module = types.ModuleType("custom_components.paperless_kiplus")
        package_module.__path__ = [
            str(ROOT / "custom_components" / "paperless_kiplus")
        ]
        const_module = types.ModuleType("custom_components.paperless_kiplus.const")
        const_module.SIGNAL_STATUS_UPDATED = "paperless_kiplus_test_signal"

        sys.modules.setdefault("custom_components", custom_components_module)
        sys.modules.setdefault("custom_components.paperless_kiplus", package_module)
        sys.modules.setdefault("custom_components.paperless_kiplus.const", const_module)

        runner_path = ROOT / "custom_components" / "paperless_kiplus" / "runner.py"
        spec = importlib.util.spec_from_file_location(
            "custom_components.paperless_kiplus.runner",
            runner_path,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Runner-Modul konnte nicht geladen werden: {runner_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        cls.build_force_stop_resume_payload = staticmethod(module.build_force_stop_resume_payload)
        cls.build_paperless_document_url = staticmethod(module.build_paperless_document_url)
        cls.infer_restart_backfill_mode = staticmethod(module.infer_restart_backfill_mode)

    def test_force_stop_payload_marks_resume_state_cleanly(self) -> None:
        payload = self.build_force_stop_resume_payload(
            {
                "kind": "progress",
                "version": 1,
                "status": "running",
                "pause_reason": None,
                "retry_after_seconds": 12.5,
                "mode": {"backfill_existing_documents": True},
                "progress": {"completed_documents": 42, "total_documents": 100},
                "completed_document_ids": [1, 2, 3],
                "pending_documents": [{"doc_id": 9, "title": "Test"}],
                "current_document": {"id": 11, "title": "Rechnung"},
            }
        )

        self.assertNotIn("kind", payload)
        self.assertEqual(payload["status"], "paused")
        self.assertEqual(payload["pause_reason"], "force_stop")
        self.assertIsNone(payload["retry_after_seconds"])
        self.assertEqual(payload["mode"]["backfill_existing_documents"], True)
        self.assertEqual(payload["progress"]["completed_documents"], 42)
        self.assertEqual(payload["pending_documents"][0]["doc_id"], 9)
        self.assertEqual(payload["current_document"]["id"], 11)
        self.assertIn("updated_at", payload)

    def test_force_stop_payload_returns_empty_for_missing_progress(self) -> None:
        self.assertEqual(self.build_force_stop_resume_payload({}), {})

    def test_build_paperless_document_url_returns_detail_link(self) -> None:
        self.assertEqual(
            self.build_paperless_document_url("https://paperless.example", 123),
            "https://paperless.example/documents/123/details",
        )

    def test_build_paperless_document_url_returns_empty_without_base_or_id(self) -> None:
        self.assertEqual(self.build_paperless_document_url("", 123), "")
        self.assertEqual(
            self.build_paperless_document_url("https://paperless.example", None),
            "",
        )

    def test_infer_restart_backfill_mode_reuses_previous_mode(self) -> None:
        self.assertTrue(
            self.infer_restart_backfill_mode(
                {"mode": {"backfill_existing_documents": True}}
            )
        )
        self.assertFalse(
            self.infer_restart_backfill_mode(
                {"mode": {"backfill_existing_documents": False}}
            )
        )

    def test_infer_restart_backfill_mode_explicit_override_wins(self) -> None:
        self.assertFalse(
            self.infer_restart_backfill_mode(
                {"mode": {"backfill_existing_documents": True}},
                explicit_backfill=False,
            )
        )
        self.assertTrue(
            self.infer_restart_backfill_mode(
                {"mode": {"backfill_existing_documents": False}},
                explicit_backfill=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
