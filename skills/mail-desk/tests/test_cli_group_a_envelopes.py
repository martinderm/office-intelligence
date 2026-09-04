"""Black-box and mocked contract tests for the OI-11a mail-desk CLIs."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = MAIL_DESK_ROOT / "scripts"
CANONICAL_KEYS = ("action", "success", "state", "message", "data", "error")


def load_preflight_module():
    spec = importlib.util.spec_from_file_location("mailbox_preflight_contract", SCRIPTS / "mailbox_preflight.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PREFLIGHT = load_preflight_module()


class MailDeskCliGroupAEnvelopeTests(unittest.TestCase):
    def assert_envelope(self, stdout: str, action: str):
        self.assertEqual(1, len(stdout.splitlines()), stdout)
        envelope = json.loads(stdout)
        self.assertEqual(CANONICAL_KEYS, tuple(envelope))
        self.assertEqual(action, envelope["action"])
        self.assertNotIn("ok", envelope)
        self.assertNotIn("status", envelope)
        self.assertNotIn("resolved", envelope)
        return envelope

    def run_script(self, name: str, args: list[str], cwd: Path, extra_env: dict[str, str] | None = None):
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_inspect_json_success_and_human_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"items": [{"message_id": "<A@example.test>", "decision": {"kind": "topic", "needs_reply": True}, "action": {"target_folder": "Themen/Test"}}]}), encoding="utf-8")

            json_run = self.run_script("mail_desk_inspect_manifest.py", ["--json", "--input", str(manifest)], root)
            self.assertEqual(0, json_run.returncode, json_run.stderr)
            envelope = self.assert_envelope(json_run.stdout, "inspect_manifest")
            self.assertTrue(envelope["success"])
            self.assertEqual("inspect", envelope["data"]["operation"])
            self.assertEqual(1, envelope["data"]["result"]["total_items_in_manifest"])
            self.assertEqual("", json_run.stderr)

            human_run = self.run_script("mail_desk_inspect_manifest.py", ["--input", str(manifest)], root)
            self.assertEqual(0, human_run.returncode, human_run.stderr)
            self.assertIn("BATCH MANIFEST INSPECTION", human_run.stdout)
            self.assertNotIn('"action"', human_run.stdout)

    def test_inspect_json_error_and_argument_diagnostic_are_canonical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = self.run_script("mail_desk_inspect_manifest.py", ["--json", "--input", str(root / "missing.json")], root)
            self.assertEqual(1, missing.returncode)
            error = self.assert_envelope(missing.stdout, "inspect_manifest")
            self.assertFalse(error["success"])
            self.assertEqual("Failed", error["state"])
            self.assertEqual("inspect", error["data"]["operation"])

            invalid = self.run_script("mail_desk_inspect_manifest.py", ["--json", "--filter-kind", "bad"], root)
            self.assertEqual(2, invalid.returncode)
            error = self.assert_envelope(invalid.stdout, "inspect_manifest")
            self.assertFalse(error["success"])
            self.assertEqual("ArgumentError", error["error"]["type"])
            self.assertIn("usage:", invalid.stderr)

    def test_final_index_cli_preserves_lookup_not_found_and_cli_only_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path = root / "data" / "mail-desk" / "final-location-index.json"
            environment = {"MAIL_DESK_FINAL_INDEX_PATH": str(index_path)}

            missing = self.run_script("mail_desk_final_location_index.py", ["lookup", "--mid", "missing@example.test"], root, environment)
            self.assertEqual(2, missing.returncode)
            error = self.assert_envelope(missing.stdout, "final_location_index")
            self.assertFalse(error["success"])
            self.assertEqual("NotFound", error["state"])
            self.assertEqual("lookup", error["data"]["operation"])

            operation = root / "index-operation.json"
            operation.write_text(json.dumps({"delete_input_on_success": False, "operations": [{"action": "upsert", "mode": "upsert-final", "payload": {"message_id": "<stored@example.test>", "final_folder": "Projects/Test", "envelope_id": "17"}}]}), encoding="utf-8")
            write_run = self.run_script("mail_desk_final_location_index.py", ["--input", str(operation)], root, environment)
            self.assertEqual(0, write_run.returncode, write_run.stderr)
            written = self.assert_envelope(write_run.stdout, "final_location_index")
            self.assertTrue(written["success"])
            self.assertEqual("manifest", written["data"]["operation"])

            found = self.run_script("mail_desk_final_location_index.py", ["lookup", "--mid", "stored@example.test"], root, environment)
            self.assertEqual(0, found.returncode, found.stderr)
            result = self.assert_envelope(found.stdout, "final_location_index")
            self.assertTrue(result["success"])
            self.assertEqual("stored@example.test", result["data"]["message_id"])

    def test_final_index_argument_error_is_one_json_and_stderr_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.run_script("mail_desk_final_location_index.py", ["lookup"], Path(temporary))
            self.assertEqual(2, result.returncode)
            envelope = self.assert_envelope(result.stdout, "final_location_index")
            self.assertFalse(envelope["success"])
            self.assertEqual("argument_parse", envelope["data"]["operation"])
            self.assertIn("usage:", result.stderr)

    def test_final_index_manifest_preserves_all_successful_operation_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path = root / "data" / "mail-desk" / "final-location-index.json"
            environment = {"MAIL_DESK_FINAL_INDEX_PATH": str(index_path)}
            operation = root / "combined-index-operation.json"
            operation.write_text(
                json.dumps(
                    {
                        "delete_input_on_success": False,
                        "operations": [
                            {"action": "stats"},
                            {
                                "action": "upsert",
                                "mode": "upsert-final",
                                "payload": {
                                    "message_id": "<combined@example.test>",
                                    "final_folder": "Projects/Test",
                                    "envelope_id": "18",
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = self.run_script("mail_desk_final_location_index.py", ["--input", str(operation)], root, environment)
            self.assertEqual(0, result.returncode, result.stderr)
            envelope = self.assert_envelope(result.stdout, "final_location_index")
            self.assertTrue(envelope["success"])
            operations = envelope["data"]["results"]
            self.assertEqual(["stats", "upsert"], [item["operation"] for item in operations])
            self.assertEqual([True, True], [item["success"] for item in operations])
            self.assertTrue(all(item["error"] is None for item in operations))

    def preflight(self, root: Path, arguments: list[str], himalaya_output: str | None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(PREFLIGHT, "run_himalaya", return_value=himalaya_output) as mocked, patch.object(sys, "argv", ["mailbox_preflight.py", *arguments]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = PREFLIGHT.main()
        return code, stdout.getvalue(), stderr.getvalue(), mocked

    def test_preflight_success_cache_and_missing_targets_are_canonical_without_mailbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            projects = root / "projects.json"
            topics = root / "topics.json"
            state = root / "state" / "preflight.json"
            projects.write_text(json.dumps([{ "id": "p", "mailbox_folder": "Projects/Test"}]), encoding="utf-8")
            topics.write_text(json.dumps([{ "id": "t", "mailbox_folder": "Topics/Test"}]), encoding="utf-8")
            args = ["--projects", str(projects), "--topics", str(topics), "--state-file", str(state)]

            code, stdout, stderr, mocked = self.preflight(root, args, '[{"name": "Projects/Test"}, {"name": "Topics/Test"}]')
            self.assertEqual(0, code)
            envelope = self.assert_envelope(stdout, "mailbox_preflight")
            self.assertTrue(envelope["success"])
            self.assertEqual("preflight", envelope["data"]["operation"])
            self.assertEqual("", stderr)
            mocked.assert_called_once()
            self.assertTrue(state.exists())

            code, stdout, _, mocked = self.preflight(root, args, None)
            self.assertEqual(0, code)
            cached = self.assert_envelope(stdout, "mailbox_preflight")
            self.assertTrue(cached["success"])
            self.assertTrue(cached["data"]["skipped"])
            mocked.assert_not_called()

            code, stdout, _, mocked = self.preflight(root, [*args, "--always"], '[{"name": "Projects/Test"}]')
            self.assertEqual(2, code)
            missing = self.assert_envelope(stdout, "mailbox_preflight")
            self.assertFalse(missing["success"])
            self.assertEqual("TargetsMissing", missing["state"])
            self.assertEqual("MissingTargets", missing["error"]["type"])
            self.assertEqual(1, missing["data"]["missing_count"])
            mocked.assert_called_once()

    def test_preflight_argument_error_is_canonical(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["mailbox_preflight.py", "--unknown"]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = PREFLIGHT.main()
        self.assertEqual(2, code)
        envelope = self.assert_envelope(stdout.getvalue(), "mailbox_preflight")
        self.assertFalse(envelope["success"])
        self.assertEqual("ArgumentError", envelope["error"]["type"])
        self.assertIn("usage:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
