"""Tests for worker config export and standalone worker helpers.

Purpose:
- Protect the new Docker/Unraid worker foundation against regressions.
- Ensure Home-Assistant config export and standalone worker config import stay
  compatible.
- Verify the worker exposes stable status and document links for UI/remote mode.

How to run:
- `python3 -m unittest discover -s tests`
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import textwrap
import threading
import types
import unittest
from urllib.request import urlopen
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class _YamlError(Exception):
    """Kleiner YAML-Fehlertyp für die lokale Testumgebung."""


def _parse_scalar(raw_value: str):
    value = raw_value.strip()
    if value == "":
        return ""
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.startswith("["):
        if not value.endswith("]"):
            raise _YamlError(f"Ungültige Flow-Liste: {value}")
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.startswith(("\"", "'")) and value.endswith(("\"", "'")) and len(value) >= 2:
        return value[1:-1]
    return value


def _simple_safe_load(text: str):
    if hasattr(text, "read"):
        text = text.read()
    payload: dict[str, object] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in raw_line:
            raise _YamlError(f"Ungültige Zeile ohne Doppelpunkt: {raw_line}")
        key, value = raw_line.split(":", 1)
        payload[key.strip()] = _parse_scalar(value)
    return payload


def _simple_safe_dump(payload, allow_unicode=True, sort_keys=False):  # noqa: ARG001
    lines = []
    items = payload.items() if isinstance(payload, dict) else []
    if sort_keys:
        items = sorted(items)
    for key, value in items:
        if isinstance(value, list):
            rendered = "[" + ", ".join(str(item) for item in value) + "]"
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines) + ("\n" if lines else "")


sys.modules["yaml"] = types.SimpleNamespace(
    safe_load=_simple_safe_load,
    safe_dump=_simple_safe_dump,
    YAMLError=_YamlError,
)


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Modul konnte nicht geladen werden: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


sys.modules.pop("paperless_ai_sorter", None)
sys.modules.pop("tax_enrichment", None)
_load_module("tax_enrichment", ROOT / "src" / "tax_enrichment.py")
_load_module("paperless_ai_sorter", ROOT / "src" / "paperless_ai_sorter.py")


config_export_module = _load_module(
    "paperless_kiplus_config_export_test",
    ROOT / "custom_components" / "paperless_kiplus" / "config_export.py",
)
worker_api_module = _load_module(
    "paperless_kiplus_worker_api_test",
    ROOT / "src" / "worker_api.py",
)

build_effective_managed_config_payload = (
    config_export_module.build_effective_managed_config_payload
)
WorkerManager = worker_api_module.WorkerManager
WorkerHttpServer = worker_api_module.WorkerHttpServer


def _collect_json_keys(payload):
    """Collect nested keys so tests can guard against accidental leaks."""

    if isinstance(payload, dict):
        keys = set(payload)
        for value in payload.values():
            keys.update(_collect_json_keys(value))
        return keys
    if isinstance(payload, list):
        keys = set()
        for item in payload:
            keys.update(_collect_json_keys(item))
        return keys
    return set()


def _request_heimdall_payload(manager: WorkerManager) -> dict[str, object]:
    """Start a local worker server and fetch the public Heimdall endpoint."""

    server = WorkerHttpServer(("127.0.0.1", 0), manager)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/api/heimdall/v1", timeout=5) as response:
            body = response.read().decode("utf-8")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise AssertionError("Heimdall response must be a JSON object.")
        return payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class ConfigExportTests(unittest.TestCase):
    """Covers the pure config export helper used by HA local and remote mode."""

    def test_effective_config_payload_applies_ui_overrides(self) -> None:
        payload = build_effective_managed_config_payload(
            textwrap.dedent(
                """
                paperless_url: https://paperless.example
                paperless_token: secret
                ai_api_key: cloud-key
                ai_model: gpt-4.1-mini
                ai_base_url: https://api.openai.com/v1
                enable_parallel_ai: false
                max_parallel_ai_jobs: 1
                tax_ai_model: qwen-old
                custom_extra_key: keep-me
                """
            ),
            input_cost_per_1k_tokens_eur=0.0004,
            output_cost_per_1k_tokens_eur=0.0016,
            already_classified_skip=True,
            already_classified_require_ki_tag=True,
            precheck_min_content_chars=240,
            precheck_min_word_count=33,
            precheck_min_alnum_ratio=0.55,
            precheck_blocked_filename_patterns="smime,.p7m",
            precheck_image_only_gate=True,
            precheck_duplicate_hash_gate=True,
            precheck_duplicate_apply_metadata=False,
            reprocess_ki_tagged_documents=False,
            enable_parallel_ai=True,
            max_parallel_ai_jobs=4,
            enable_tax_enrichment=True,
            tax_process_ki_tagged_documents=True,
            tax_personal_context="Haushalt mit Kindern",
        )

        self.assertEqual(payload["precheck_min_content_chars"], 240)
        self.assertEqual(payload["precheck_min_word_count"], 33)
        self.assertEqual(payload["precheck_blocked_filename_patterns"], ["smime", ".p7m"])
        self.assertTrue(payload["enable_parallel_ai"])
        self.assertEqual(payload["max_parallel_ai_jobs"], 4)
        self.assertEqual(payload["tax_personal_context"], "Haushalt mit Kindern")
        self.assertEqual(payload["custom_extra_key"], "keep-me")
        self.assertEqual(payload["tax_ai_model"], "qwen-old")


class WorkerManagerTests(unittest.TestCase):
    """Covers the standalone worker without needing a live HTTP server."""

    def _valid_yaml(self) -> str:
        return textwrap.dedent(
            """
            paperless_url: https://paperless.example
            paperless_token: token-123
            ai_api_key: openai-key
            ai_model: gpt-4.1-mini
            ai_base_url: https://api.openai.com/v1
            enable_tax_enrichment: true
            tax_ai_api_key: dummy
            tax_ai_model: qwen2.5:7b
            tax_ai_base_url: http://ollama:11434/v1
            """
        ).strip()

    def test_import_config_yaml_updates_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkerManager(
                data_dir=Path(tmp_dir),
                sorter_command=["python3", "-c", "print('ok')"],
                auth_token="",
            )

            result = manager.import_config_yaml(self._valid_yaml(), source="unit_test")
            status = manager.status_payload()

            self.assertTrue(result["config_validation_ok"])
            self.assertEqual(status["config_source"], "unit_test")
            self.assertEqual(status["paperless_base_url"], "https://paperless.example")
            self.assertEqual(status["config_validation_message"], "Konfiguration ist gültig.")

    def test_import_config_yaml_rejects_invalid_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkerManager(
                data_dir=Path(tmp_dir),
                sorter_command=["python3", "-c", "print('ok')"],
                auth_token="",
            )

            with self.assertRaises(ValueError):
                manager.import_config_yaml("paperless_url: [1,", source="unit_test")

    def test_runtime_payload_builds_clickable_document_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkerManager(
                data_dir=Path(tmp_dir),
                sorter_command=["python3", "-c", "print('ok')"],
                auth_token="",
            )
            manager.import_config_yaml(self._valid_yaml(), source="unit_test")

            manager._apply_runtime_payload(
                {
                    "kind": "progress",
                    "updated_at": "2026-04-26T18:00:00+00:00",
                    "current_document": {"id": 5366, "title": "Neue Rechnung"},
                    "completed_document_ids": [5366],
                    "progress": {
                        "total_documents": 100,
                        "completed_documents": 15,
                        "percent": 15.0,
                        "scanned": 16,
                        "updated": 1,
                        "skipped": 14,
                        "failed": 0,
                    },
                }
            )

            status = manager.status_payload()
            self.assertEqual(
                status["progress_current_document_url"],
                "https://paperless.example/documents/5366/details",
            )
            self.assertEqual(
                status["last_completed_document_url"],
                "https://paperless.example/documents/5366/details",
            )
            self.assertEqual(status["progress_completed_documents"], 15)

    def test_heimdall_endpoint_returns_public_happy_path_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkerManager(
                data_dir=Path(tmp_dir),
                sorter_command=["python3", "-c", "print('ok')"],
                auth_token="secret-worker-token",
            )
            payload = _request_heimdall_payload(manager)

            self.assertEqual(payload["status"], "ok")
            self.assertIn("summary", payload)
            self.assertIsInstance(payload["stats"], list)
            self.assertIsInstance(payload["details"], list)

    def test_heimdall_endpoint_has_at_least_one_stat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkerManager(
                data_dir=Path(tmp_dir),
                sorter_command=["python3", "-c", "print('ok')"],
                auth_token="secret-worker-token",
            )
            payload = _request_heimdall_payload(manager)

            self.assertGreaterEqual(len(payload["stats"]), 1)

    def test_heimdall_endpoint_omits_sensitive_fields_and_raw_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = WorkerManager(
                data_dir=Path(tmp_dir),
                sorter_command=["python3", "-c", "print('ok')"],
                auth_token="secret-worker-token",
            )
            manager.last_stdout_tail = "API_KEY=secret"
            manager.last_stderr_tail = "Traceback with token"
            manager.log_lines = ["Cookie: secret", "Authorization: Bearer secret"]

            payload = _request_heimdall_payload(manager)
            rendered = json.dumps(payload, ensure_ascii=False).lower()
            keys = {key.lower() for key in _collect_json_keys(payload)}

            self.assertNotIn("secret-worker-token", rendered)
            self.assertNotIn("api_key", rendered)
            self.assertNotIn("authorization", rendered)
            self.assertNotIn("cookie", rendered)
            self.assertNotIn("traceback", rendered)
            self.assertNotIn("log_text", keys)
            self.assertNotIn("stdout_tail", keys)
            self.assertNotIn("stderr_tail", keys)


if __name__ == "__main__":
    unittest.main()
