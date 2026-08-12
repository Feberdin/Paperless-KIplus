"""Tests for Home Assistant compatibility with HTTP-202 worker jobs.

Purpose:
- Protect the remote runner's legacy/202 response adapter and terminal polling.
- Verify that HA actions send an idempotency key without a Home Assistant install.

Input / Output:
- Input: deterministic fake worker responses and minimal HA/aiohttp modules.
- Output: assertions against the runner fields consumed by HA entities.

Important invariants:
- A queued job keeps HA polling even before the sorter process is visible.
- Terminal job errors must not be overwritten by a later worker-status refresh.
- Existing legacy immediate responses remain supported.

How to debug:
- Run `python3 -m unittest tests.test_remote_runner_jobs -v`.
- Inspect `_active_job_status_url`, `last_status`, and `last_message` on failure.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "custom_components" / "paperless_kiplus"


def _load_remote_runner_module():
    """Load the runner with only the HA interfaces used during import."""

    aiohttp_module = types.ModuleType("aiohttp")
    aiohttp_module.ClientTimeout = lambda **kwargs: kwargs
    sys.modules.setdefault("aiohttp", aiohttp_module)

    homeassistant_module = types.ModuleType("homeassistant")
    core_module = types.ModuleType("homeassistant.core")
    helpers_module = types.ModuleType("homeassistant.helpers")
    aiohttp_client_module = types.ModuleType("homeassistant.helpers.aiohttp_client")
    dispatcher_module = types.ModuleType("homeassistant.helpers.dispatcher")

    class _FakeHomeAssistant:
        """Minimal type stub required by the module annotations."""

    core_module.HomeAssistant = _FakeHomeAssistant
    aiohttp_client_module.async_get_clientsession = lambda _hass: None
    dispatcher_module.async_dispatcher_send = lambda *_args, **_kwargs: None
    homeassistant_module.core = core_module
    homeassistant_module.helpers = helpers_module
    helpers_module.aiohttp_client = aiohttp_client_module
    helpers_module.dispatcher = dispatcher_module

    sys.modules.setdefault("homeassistant", homeassistant_module)
    sys.modules.setdefault("homeassistant.core", core_module)
    sys.modules.setdefault("homeassistant.helpers", helpers_module)
    sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client_module)
    sys.modules.setdefault("homeassistant.helpers.dispatcher", dispatcher_module)

    custom_components_module = types.ModuleType("custom_components")
    package_module = types.ModuleType("custom_components.paperless_kiplus")
    package_module.__path__ = [str(PACKAGE_DIR)]
    const_module = types.ModuleType("custom_components.paperless_kiplus.const")
    const_module.SIGNAL_STATUS_UPDATED = "paperless_kiplus_test_signal"
    sys.modules.setdefault("custom_components", custom_components_module)
    sys.modules.setdefault("custom_components.paperless_kiplus", package_module)
    sys.modules.setdefault("custom_components.paperless_kiplus.const", const_module)

    module_name = "custom_components.paperless_kiplus.remote_runner"
    spec = importlib.util.spec_from_file_location(module_name, PACKAGE_DIR / "remote_runner.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Remote-Runner-Modul konnte nicht geladen werden.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


REMOTE_MODULE = _load_remote_runner_module()
RemotePaperlessRunner = REMOTE_MODULE.RemotePaperlessRunner


def _bare_runner():
    """Build only the state needed by the methods under test."""

    runner = RemotePaperlessRunner.__new__(RemotePaperlessRunner)
    runner._active_job_status_url = ""
    runner._active_job_request_id = ""
    runner._poll_task = None
    runner._lock = asyncio.Lock()
    runner.running = False
    runner.resume_available = False
    runner.last_status = "idle"
    runner.last_message = "not started"
    runner.last_stderr_tail = ""
    runner.last_exit_code = None
    runner.remote_worker_sync_config = False
    runner.managed_config_enabled = False
    runner.default_dry_run = False
    runner.default_all_documents = False
    runner.default_max_documents = 0
    runner._notify = lambda: None
    return runner


class RemoteRunnerJobTests(unittest.TestCase):
    """Covers 202 admission, polling, failures, and legacy compatibility."""

    def test_202_admission_keeps_runner_active(self) -> None:
        runner = _bare_runner()

        runner._apply_action_response(
            {
                "status": "queued",
                "job_id": "job_123",
                "request_id": "req_123",
                "status_url": "/api/jobs/job_123",
            }
        )

        self.assertTrue(runner.running)
        self.assertEqual(runner.last_status, "queued")
        self.assertEqual(runner._active_job_status_url, "/api/jobs/job_123")
        self.assertIn("req_123", runner.last_message)

    def test_legacy_immediate_response_remains_supported(self) -> None:
        runner = _bare_runner()
        applied: list[dict[str, object]] = []
        runner._apply_status_payload = applied.append

        runner._apply_action_response(
            {"ok": True, "status": {"running": True, "status": "running"}}
        )

        self.assertEqual(applied, [{"running": True, "status": "running"}])

    def test_terminal_failure_survives_worker_status_refresh(self) -> None:
        runner = _bare_runner()
        runner.running = True
        runner._active_job_status_url = "/api/jobs/job_failed"
        runner._active_job_request_id = "req_failed"

        async def api_json(_method, _path):
            return {
                "status": "failed",
                "error": {"message": "Sichere Jobmeldung.", "request_id": "req_failed"},
            }

        async def refresh_status():
            runner.running = False
            runner.last_status = "idle"
            runner.last_message = "worker idle"

        runner._api_json = api_json
        runner._refresh_status = refresh_status

        asyncio.run(runner._poll_loop())

        self.assertEqual(runner.last_status, "remote_job_failed")
        self.assertEqual(runner.last_message, "Sichere Jobmeldung.")
        self.assertEqual(runner._active_job_status_url, "")

    def test_async_run_sends_idempotency_key(self) -> None:
        runner = _bare_runner()
        captured: dict[str, object] = {}

        async def api_json(method, path, *, payload=None, idempotency_key=None):
            captured.update(
                method=method,
                path=path,
                payload=payload,
                idempotency_key=idempotency_key,
            )
            return {
                "status": "queued",
                "job_id": "job_123",
                "request_id": "req_123",
                "status_url": "/api/jobs/job_123",
            }

        runner._api_json = api_json
        runner._ensure_polling = lambda: None

        asyncio.run(runner.async_run(dry_run=True, max_documents=3))

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["path"], "/api/run")
        self.assertEqual(captured["payload"]["max_documents"], 3)
        self.assertRegex(str(captured["idempotency_key"]), r"^[0-9a-f]{32}$")


if __name__ == "__main__":
    unittest.main()
