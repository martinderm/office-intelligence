import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gen_filemap.py"
SPEC = importlib.util.spec_from_file_location("cloud_atlas_gen_filemap_contract", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FilemapContractTests(unittest.TestCase):
    keys = {"action", "success", "state", "message", "data", "error"}

    def run_main(self, *arguments, side_effect=None):
        stdout = io.StringIO()
        patcher = mock.patch.object(MODULE, "run_generation", side_effect=side_effect) if side_effect is not None else contextlib.nullcontext()
        with mock.patch.object(MODULE.sys, "argv", ["gen_filemap.py", *arguments]), patcher, contextlib.redirect_stdout(stdout):
            exit_code = MODULE.main()
        return exit_code, stdout.getvalue()

    def result(self, stdout):
        result = json.loads(stdout)
        self.assertEqual(self.keys, set(result))
        self.assertEqual(MODULE.ACTION, result["action"])
        return result

    def run_real(self, root, *arguments):
        stdout = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", ["gen_filemap.py", *arguments, "--workspace-root", str(root)]), contextlib.redirect_stdout(stdout):
            exit_code = MODULE.main()
        return exit_code, stdout.getvalue()

    def test_json_success_has_storage_outputs_and_zero_exit(self):
        data = {"target": {"kind": "project", "id": "example"}, "storages": [{"storage_id": "default", "output_json": "memory/cloud/projects/example/filemap.json", "output_md": "memory/cloud/projects/example/filemap.md", "file_count": 2}]}
        exit_code, stdout = self.run_main("--project-id", "example", "--json", side_effect=lambda args: data)
        result = self.result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        self.assertEqual("Completed", result["state"])
        self.assertEqual(2, result["data"]["storages"][0]["file_count"])

    def test_json_config_and_scan_errors_are_structured(self):
        exit_code, stdout = self.run_main("--project-id", "example", "--json", side_effect=RuntimeError("No cloud sync configurations resolved for ID 'example'."))
        result = self.result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("config", result["error"]["phase"])

        scan_error = MODULE.GenerationError("scan", "archive", FileNotFoundError("Scan directory missing"))
        exit_code, stdout = self.run_main("--project-id", "example", "--json", side_effect=scan_error)
        result = self.result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("scan", result["error"]["phase"])
        self.assertEqual("archive", result["error"]["storage_id"])

    def test_real_missing_config_and_scan_path_fail_without_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            exit_code, stdout = self.run_real(root, "--project-id", "example", "--storage-id", "missing", "--json")
            result = self.result(stdout)
            self.assertEqual(1, exit_code)
            self.assertEqual("config", result["error"]["phase"])

            projects = root / "memory" / "references" / "projects"
            projects.mkdir(parents=True)
            (projects / "projects.json").write_text(json.dumps([{"id": "example", "cloud_sync": {"archive": {"scan_dir": "data/cloud/absent"}}}]), encoding="utf-8")
            exit_code, stdout = self.run_real(root, "--project-id", "example", "--json")
            result = self.result(stdout)
            self.assertEqual(1, exit_code)
            self.assertEqual("scan", result["error"]["phase"])
            self.assertEqual("archive", result["error"]["storage_id"])

    def test_json_validation_and_write_errors_are_structured(self):
        for failure, phase in ((ValueError("bad schema"), "validation"), (OSError("write failed"), "write")):
            with self.subTest(phase=phase):
                exit_code, stdout = self.run_main("--project-id", "example", "--json", side_effect=failure)
                result = self.result(stdout)
                self.assertEqual(1, exit_code)
                self.assertEqual(phase, result["error"]["phase"])

    def test_multistorage_failure_preserves_completed_storage_results(self):
        completed = [{"storage_id": "first", "output_json": "memory/cloud/projects/example/filemap-first.json", "output_md": "memory/cloud/projects/example/filemap-first.md", "file_count": 1}]
        failure = MODULE.GenerationError("write", "second", OSError("second output failed"), completed)
        exit_code, stdout = self.run_main("--project-id", "example", "--json", side_effect=failure)
        result = self.result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("second", result["error"]["storage_id"])
        self.assertEqual(completed, result["data"]["storages"])

    def test_markdown_write_failure_reports_current_partial_json_output(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for storage_id in ("first", "second"):
                directory = root / "data" / "cloud" / storage_id.upper()
                directory.mkdir(parents=True)
                (directory / "note.txt").write_text(storage_id, encoding="utf-8")
            projects = root / "memory" / "references" / "projects"
            projects.mkdir(parents=True)
            (projects / "projects.json").write_text(json.dumps([{"id": "example", "cloud_sync": {
                "first": {"scan_dir": "data/cloud/FIRST", "output_json": "memory/cloud/projects/example/filemap-first.json", "output_md": "memory/cloud/projects/example/filemap-first.md"},
                "second": {"scan_dir": "data/cloud/SECOND", "output_json": "memory/cloud/projects/example/filemap-second.json", "output_md": "memory/cloud/projects/example/filemap-second.md"},
            }}]), encoding="utf-8")
            real_write = MODULE.atomic_write_text

            def fail_second_markdown(filepath, content):
                if Path(filepath).name == "filemap-second.md":
                    raise OSError("simulated markdown write failure")
                return real_write(filepath, content)

            with mock.patch.object(MODULE, "atomic_write_text", side_effect=fail_second_markdown):
                exit_code, stdout = self.run_real(root, "--project-id", "example", "--json")
            result = self.result(stdout)
            self.assertEqual(1, exit_code)
            self.assertFalse(result["success"])
            self.assertEqual("write", result["error"]["phase"])
            self.assertEqual("second", result["error"]["storage_id"])
            self.assertEqual("first", result["data"]["storages"][0]["storage_id"])
            self.assertEqual("second", result["data"]["partial_storage"]["storage_id"])
            self.assertEqual(["json"], result["data"]["partial_storage"]["completed_outputs"])
            self.assertTrue((root / "memory" / "cloud" / "projects" / "example" / "filemap-second.json").is_file())

    def test_catalog_warning_has_the_same_human_and_json_semantics(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "data" / "cloud" / "EXAMPLE").mkdir(parents=True)
            (root / "data" / "cloud" / "EXAMPLE" / "note.txt").write_text("note", encoding="utf-8")
            projects = root / "memory" / "references" / "projects"
            projects.mkdir(parents=True)
            (projects / "projects.json").write_text(json.dumps([{"id": "example", "cloud_sync": {"default": {"scan_dir": "data/cloud/EXAMPLE"}}}]), encoding="utf-8")

            def warning(*args, **kwargs):
                message = "Could not update last_synced_at: " + ("x" * 1200)
                print(f"Warning: {message}")
                return message

            with mock.patch.object(MODULE, "update_config_last_synced_at", side_effect=warning):
                exit_code, json_stdout = self.run_real(root, "--project-id", "example", "--json")
                json_result = self.result(json_stdout)
                self.assertEqual(0, exit_code)
                warning_message = json_result["data"]["warnings"][0]["message"]
                self.assertLessEqual(len(warning_message), MODULE.MAX_ENVELOPE_MESSAGE_LENGTH)
                self.assertTrue(warning_message.endswith("..."))
                self.assertNotIn("Warning:", json_stdout)

                exit_code, human_stdout = self.run_real(root, "--project-id", "example")
                self.assertEqual(0, exit_code)
                self.assertIn("Warning: Could not update last_synced_at", human_stdout)

    def test_human_error_is_concise_and_has_no_traceback(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", ["gen_filemap.py", "--project-id", "example"]), mock.patch.object(MODULE, "run_generation", side_effect=ValueError("bad schema")), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = MODULE.main()
        self.assertEqual(1, exit_code)
        self.assertIn("Error: bad schema", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_json_argument_error_is_structured(self):
        exit_code, stdout = self.run_main("--json")
        result = self.result(stdout)
        self.assertEqual(2, exit_code)
        self.assertEqual("arguments", result["error"]["phase"])

    def test_json_stdout_stays_pure_when_generation_is_noisy(self):
        def noisy(args):
            print("Warning: simulated progress")
            return {"target": {"kind": "project", "id": "example"}, "storages": []}

        exit_code, stdout = self.run_main("--project-id", "example", "--json", side_effect=noisy)
        result = self.result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        self.assertNotIn("simulated progress", stdout)

    def test_human_mode_remains_readable_without_envelope(self):
        def visible(args):
            print("Generiere sichtbaren Fortschritt")
            return {"target": {"kind": "project", "id": "example"}, "storages": []}

        exit_code, stdout = self.run_main("--project-id", "example", side_effect=visible)
        self.assertEqual(0, exit_code)
        self.assertIn("sichtbaren Fortschritt", stdout)
        self.assertFalse(stdout.lstrip().startswith("{"))


if __name__ == "__main__":
    unittest.main()
