"""Exercise actual export methods without installing the Home Assistant runtime.

Input: synthetic logs and a temporary media directory, never a production account.
Output: protected file paths and notification URLs. Run with unittest discovery.
The AST loader isolates the public method; only HA and remote I/O are fake.
"""

import ast
from datetime import UTC, datetime
import importlib.util
from pathlib import Path, PurePosixPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/paperless_kiplus"


class RecordingPath(PurePosixPath):
    """Prevent the legacy method from writing outside the test directory."""

    def mkdir(self, **kwargs):
        pass

    def write_text(self, text, **kwargs):
        return len(text)


def load_export_method(filename, method_name="async_export_last_log"):
    """Compile unchanged production method, replacing only system boundaries."""
    tree = ast.parse((COMPONENT / filename).read_text())
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == method_name
    )
    namespace = {
        "Path": RecordingPath, "datetime": datetime, "UTC": UTC,
        "build_effective_managed_config_yaml": lambda *a, **k: "mode: synthetic\n",
    }
    helper = COMPONENT / "private_exports.py"
    if helper.exists():
        spec = importlib.util.spec_from_file_location("private_exports_test", helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        namespace["export_destination"] = module.export_destination
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(COMPONENT / filename), "exec"), namespace)
    return namespace[method_name]


class SyntheticRunner(SimpleNamespace):
    """Unrelated worker settings are inert; export logic remains production code."""

    def __getattr__(self, name):
        return 0


class PrivateExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_view_authorizes_admin_and_disables_caching(self):
        spec = importlib.util.spec_from_file_location("private_exports_test", COMPONENT / "private_exports.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tree = ast.parse((COMPONENT / "export_http.py").read_text())
        view_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        unauthorized = type("Unauthorized", (Exception,), {})
        forbidden = type("Forbidden", (Exception,), {})
        not_found = type("NotFound", (Exception,), {})
        namespace = {
            "HomeAssistantView": object,
            "export_destination": module.export_destination,
            "web": SimpleNamespace(
                HTTPUnauthorized=unauthorized, HTTPForbidden=forbidden,
                HTTPNotFound=not_found,
                FileResponse=lambda path, headers: SimpleNamespace(path=path, headers=headers),
            ),
        }
        exec(compile(ast.Module(body=[view_class], type_ignores=[]), "export_http.py", "exec"), namespace)
        with tempfile.TemporaryDirectory() as folder:
            async def executor(job, *args):
                return job(*args)

            hass = SimpleNamespace(config=SimpleNamespace(media_dirs={"local": folder}), async_add_executor_job=executor)
            view = namespace["ExportDownloadView"](hass)
            self.assertTrue(view.requires_auth)
            filename = "paperless_kiplus_last_log.txt"
            for request, exception in (({}, unauthorized), ({"hass_user": SimpleNamespace(is_admin=False)}, forbidden)):
                with self.assertRaises(exception):
                    await view.get(request, filename)
            admin = {"hass_user": SimpleNamespace(is_admin=True)}
            for name in (filename, "../../secrets.yaml"):
                with self.assertRaises(not_found):
                    await view.get(admin, name)
            path, _ = module.export_destination(hass, filename)
            path.parent.mkdir()
            path.write_text("synthetic")
            response = await view.get(admin, filename)
            self.assertEqual(response.path, path)
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertIn("attachment", response.headers["Content-Disposition"])

    async def test_both_log_exporters_use_authenticated_download_endpoint(self):
        for filename in ("runner.py", "remote_runner.py"):
            with self.subTest(exporter=filename), tempfile.TemporaryDirectory() as folder:
                async def executor(job):
                    return job()

                hass = SimpleNamespace(
                    config=SimpleNamespace(media_dirs={"private": folder}),
                    async_add_executor_job=executor,
                    services=SimpleNamespace(async_call=AsyncMock()),
                )
                runner = SimpleNamespace(
                    hass=hass, last_log_combined="Synthetic log",
                    _api_text=AsyncMock(return_value="Synthetic log"),
                    _notify=Mock(),
                )
                url = await load_export_method(filename)(runner)
                self.assertTrue(
                    url.startswith("/api/paperless_kiplus/exports/"),
                    f"{filename} exposes private logs through {url.split('?')[0]}",
                )
                self.assertTrue(Path(runner.last_log_export_path).is_relative_to(Path(folder).resolve()))
                self.assertEqual(Path(runner.last_log_export_path).read_text(), "Synthetic log")
                runner.hass.services.async_call.assert_awaited_once()

    async def test_both_configuration_exporters_avoid_public_files(self):
        for filename in ("runner.py", "remote_runner.py"):
            with self.subTest(exporter=filename), tempfile.TemporaryDirectory() as folder:
                async def executor(job):
                    return job()

                hass = SimpleNamespace(
                    config=SimpleNamespace(media_dirs={"private": folder}),
                    async_add_executor_job=executor,
                    services=SimpleNamespace(async_call=AsyncMock()),
                )
                runner = SyntheticRunner(
                    hass=hass, _notify=Mock(),
                    _effective_config_yaml=lambda: "mode: synthetic\n",
                )
                await load_export_method(filename, "async_export_worker_config")(
                    runner, remote_upload=False, announce=True,
                )
                path = Path(folder) / "paperless_kiplus/paperless_kiplus_worker_config.yaml"
                self.assertTrue(path.exists(), f"{filename} did not write configuration to protected media")
                self.assertEqual(path.read_text(), "mode: synthetic\n")
                message = hass.services.async_call.await_args.args[2]["message"]
                self.assertIn("/api/paperless_kiplus/exports/", message)
                self.assertNotIn("/local/", message)

    def test_export_destination_rejects_public_or_missing_media_and_path_traversal(self):
        spec = importlib.util.spec_from_file_location("private_exports_test", COMPONENT / "private_exports.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for directories, filename in (
            ({}, "paperless_kiplus_last_log.txt"),
            ({"local": "/config/www"}, "paperless_kiplus_last_log.txt"),
            ({"local": "/media"}, "../../www/export.yaml"),
        ):
            with self.subTest(directories=directories, filename=filename):
                hass = SimpleNamespace(config=SimpleNamespace(media_dirs=directories))
                with self.assertRaises(ValueError):
                    module.export_destination(hass, filename)

    def test_standard_local_media_is_supported_without_custom_configuration(self):
        spec = importlib.util.spec_from_file_location("private_exports_test", COMPONENT / "private_exports.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            hass = SimpleNamespace(config=SimpleNamespace(media_dirs={"local": folder}))
            path, url = module.export_destination(hass, "paperless_kiplus_last_log.txt")
            self.assertTrue(path.is_relative_to(Path(folder).resolve()))
            self.assertEqual(url, "/api/paperless_kiplus/exports/paperless_kiplus_last_log.txt")


if __name__ == "__main__":
    unittest.main()
