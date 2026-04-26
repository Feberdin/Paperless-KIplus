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


if __name__ == "__main__":
    unittest.main()
