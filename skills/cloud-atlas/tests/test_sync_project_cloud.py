import contextlib
import importlib.util
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sync_project_cloud.py"
SPEC = importlib.util.spec_from_file_location("cloud_atlas_sync_project_cloud", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SyncProjectCloudContractTests(unittest.TestCase):
    allowed_keys = {"action", "success", "state", "message", "data", "error"}

    def run_main(self, *arguments, side_effect=None):
        stdout = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", ["sync_project_cloud.py", *arguments]), mock.patch.object(
            MODULE.subprocess, "run", side_effect=side_effect
        ) as run_mock, contextlib.redirect_stdout(stdout):
            exit_code = MODULE.main()
        return exit_code, stdout.getvalue(), run_mock

    def json_result(self, stdout):
        result = json.loads(stdout)
        self.assertEqual(self.allowed_keys, set(result))
        self.assertEqual(MODULE.ACTION, result["action"])
        return result

    def test_json_success_is_canonical_and_returns_zero(self):
        completed = subprocess.CompletedProcess([], 0)
        exit_code, stdout, run_mock = self.run_main("--project-id", "example", "--json", side_effect=[completed, completed])

        result = self.json_result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        self.assertEqual("Completed", result["state"])
        self.assertEqual(["convert", "filemap"], result["data"]["completed_steps"])
        self.assertIsNone(result["error"])
        self.assertTrue(all(call.kwargs["capture_output"] for call in run_mock.call_args_list))

    def test_converter_error_stops_before_filemap(self):
        exit_code, stdout, run_mock = self.run_main(
            "--project-id", "example", "--json", side_effect=[subprocess.CompletedProcess([], 17)]
        )

        result = self.json_result(stdout)
        self.assertEqual(17, exit_code)
        self.assertFalse(result["success"])
        self.assertEqual("Failed", result["state"])
        self.assertEqual("convert", result["error"]["step"])
        self.assertEqual(17, result["error"]["exit_code"])
        self.assertEqual([], result["data"]["completed_steps"])
        self.assertEqual(1, run_mock.call_count)

    def test_conversion_required_child_is_preserved_as_typed_envelope(self):
        child = {
            "action": "convert_cloud_docs",
            "success": False,
            "state": "ConversionRequired",
            "message": "Conversion requires attention for 1 file(s).",
            "data": {"storages": []},
            "error": {"type": "ConversionRequired", "requirements": [{"capability": "markitdown"}]},
        }
        conversion = subprocess.CompletedProcess([], 1, stdout=json.dumps(child))
        filemap = subprocess.CompletedProcess([], 0, stdout=json.dumps({"success": True}))

        exit_code, stdout, run_mock = self.run_main("--project-id", "example", "--json", side_effect=[conversion, filemap])

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertFalse(result["success"])
        self.assertEqual("ConversionRequired", result["state"])
        self.assertEqual("ConversionRequired", result["error"]["type"])
        self.assertEqual(child["error"], result["error"]["details"])
        self.assertEqual(child["error"]["requirements"], result["error"]["requirements"])
        self.assertEqual(["convert", "filemap"], result["data"]["completed_steps"])
        self.assertEqual(2, run_mock.call_count)
        self.assertTrue(all("--json" in call.args[0] for call in run_mock.call_args_list))

    def test_filemap_failure_is_not_masked_by_conversion_required(self):
        child = {
            "state": "ConversionRequired",
            "message": "Conversion requires attention for 1 file(s).",
            "error": {"type": "ConversionRequired"},
        }
        conversion = subprocess.CompletedProcess([], 1, stdout=json.dumps(child))
        filemap = subprocess.CompletedProcess([], 9, stdout=json.dumps({"success": False}))

        exit_code, stdout, run_mock = self.run_main("--project-id", "example", "--json", side_effect=[conversion, filemap])

        result = self.json_result(stdout)
        self.assertEqual(9, exit_code)
        self.assertEqual("Failed", result["state"])
        self.assertEqual("filemap", result["error"]["step"])
        self.assertEqual(["convert"], result["data"]["completed_steps"])
        self.assertEqual(2, run_mock.call_count)

    def test_filemap_error_returns_nonzero(self):
        exit_code, stdout, run_mock = self.run_main(
            "--topic-id", "example", "--json", side_effect=[subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 9)]
        )

        result = self.json_result(stdout)
        self.assertEqual(9, exit_code)
        self.assertEqual("filemap", result["error"]["step"])
        self.assertEqual(["convert"], result["data"]["completed_steps"])
        self.assertEqual(2, run_mock.call_count)

    def test_oserror_is_structured(self):
        exit_code, stdout, _ = self.run_main("--project-id", "example", "--json", side_effect=OSError("launcher unavailable"))

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("convert", result["error"]["step"])
        self.assertIsNone(result["error"]["exit_code"])
        self.assertEqual("OSError", result["error"]["type"])

    def test_json_argument_error_is_structured(self):
        exit_code, stdout, run_mock = self.run_main("--json")

        result = self.json_result(stdout)
        self.assertEqual(2, exit_code)
        self.assertEqual("arguments", result["error"]["step"])
        self.assertEqual(2, result["error"]["exit_code"])
        self.assertEqual(0, run_mock.call_count)

    def test_json_stdout_stays_pure_when_child_writes_output(self):
        def noisy_child(*args, **kwargs):
            self.assertTrue(kwargs["capture_output"])
            return subprocess.CompletedProcess(args[0], 0, stdout="simulated child output", stderr="simulated child error")

        exit_code, stdout, _ = self.run_main("--project-id", "example", "--json", side_effect=noisy_child)

        result = self.json_result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        self.assertNotIn("simulated child output", stdout)

    def test_human_mode_remains_readable_without_envelope(self):
        def visible_child(*args, **kwargs):
            self.assertEqual({}, kwargs)
            print("child progress")
            return subprocess.CompletedProcess(args[0], 0)

        exit_code, stdout, run_mock = self.run_main("--project-id", "example", side_effect=visible_child)

        self.assertEqual(0, exit_code)
        self.assertIn("Schritt 1", stdout)
        self.assertIn("Schritt 2", stdout)
        self.assertIn("child progress", stdout)
        self.assertIn("erfolgreich abgeschlossen", stdout)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertTrue(all(not call.kwargs for call in run_mock.call_args_list))


if __name__ == "__main__":
    unittest.main()
