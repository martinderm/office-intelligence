"""Contract tests for the OI-11b mail-desk command-line tools."""

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

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def load_module(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MOVE_AND_PATCH = load_module("mail_desk_move_and_patch.py", "move_and_patch_contract")
HIMALAYA_CLIENT = load_module("mail_desk_himalaya_client.py", "himalaya_client_contract")


class MailDeskCliGroupBEnvelopeTests(unittest.TestCase):
    def assert_envelope(self, stdout: str, action: str):
        self.assertEqual(1, len(stdout.splitlines()), stdout)
        envelope = json.loads(stdout)
        self.assertEqual(CANONICAL_KEYS, tuple(envelope))
        self.assertEqual(action, envelope["action"])
        self.assertNotIn("ok", envelope)
        self.assertNotIn("status", envelope)
        self.assertNotIn("resolved", envelope)
        return envelope

    def run_script(self, filename: str, arguments: list[str], cwd: Path):
        return subprocess.run(
            [sys.executable, str(SCRIPTS / filename), *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )

    def invoke_module(self, module, filename: str, arguments: list[str]):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", [filename, *arguments]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_resolve_case_success_not_found_and_argument_error_preserve_archiving(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            (data_dir / "replies-needed.jsonl").write_text(
                json.dumps({"message_id": "<case@example.test>", "needs_reply": True}) + "\n",
                encoding="utf-8",
            )

            success = self.run_script(
                "mail_desk_resolve_case.py",
                [
                    "--message-id", "<case@example.test>",
                    "--resolution", "Reply received.",
                    "--data-dir", str(data_dir),
                ],
                root,
            )
            self.assertEqual(0, success.returncode, success.stderr)
            envelope = self.assert_envelope(success.stdout, "resolve_case")
            self.assertTrue(envelope["success"])
            self.assertEqual("resolve", envelope["data"]["operation"])
            self.assertEqual("case@example.test", envelope["data"]["message_id"])
            self.assertEqual("replies-needed.jsonl", envelope["data"]["source_file"])
            self.assertEqual("resolved", envelope["data"]["archived_item"]["status"])
            self.assertEqual("", (data_dir / "replies-needed.jsonl").read_text(encoding="utf-8"))
            self.assertTrue(Path(envelope["data"]["archived_to"]).exists())

            missing = self.run_script(
                "mail_desk_resolve_case.py",
                ["--message-id", "missing@example.test", "--resolution", "No case.", "--data-dir", str(data_dir)],
                root,
            )
            self.assertEqual(2, missing.returncode)
            envelope = self.assert_envelope(missing.stdout, "resolve_case")
            self.assertFalse(envelope["success"])
            self.assertEqual("NotFound", envelope["state"])
            self.assertEqual("resolve", envelope["data"]["operation"])

            invalid = self.run_script("mail_desk_resolve_case.py", ["--message-id", "case@example.test"], root)
            self.assertEqual(2, invalid.returncode)
            envelope = self.assert_envelope(invalid.stdout, "resolve_case")
            self.assertEqual("ArgumentError", envelope["error"]["type"])
            self.assertEqual("argument_parse", envelope["data"]["operation"])
            self.assertIn("usage:", invalid.stderr)

    def test_move_and_patch_success_not_found_and_late_index_failure_expose_partial_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path = root / "final-location-index.json"
            arguments = ["--message-id", "<move@example.test>", "--target-folder", "Projects/Test", "--index", str(index_path)]
            index_item = {"items": {"move@example.test": {"final_folder": "INBOX", "envelope_id": "11"}}}

            with patch.object(MOVE_AND_PATCH, "load_final_index", return_value=index_item), patch.object(MOVE_AND_PATCH, "run_himalaya") as run_himalaya, patch.object(MOVE_AND_PATCH, "verify_in_target_folder", return_value="22"), patch.object(MOVE_AND_PATCH, "upsert_final_index_entry") as upsert:
                code, stdout, stderr = self.invoke_module(MOVE_AND_PATCH, "mail_desk_move_and_patch.py", arguments)
            self.assertEqual(0, code, stderr)
            envelope = self.assert_envelope(stdout, "move_and_patch")
            self.assertTrue(envelope["success"])
            self.assertTrue(envelope["data"]["mailbox"]["copy_completed"])
            self.assertTrue(envelope["data"]["mailbox"]["target_verified"])
            self.assertTrue(envelope["data"]["index"]["updated"])
            self.assertEqual(2, run_himalaya.call_count)
            upsert.assert_called_once()

            with patch.object(MOVE_AND_PATCH, "load_final_index", return_value={"items": {}}), patch.object(MOVE_AND_PATCH, "search_mailbox", return_value=[]):
                code, stdout, _ = self.invoke_module(MOVE_AND_PATCH, "mail_desk_move_and_patch.py", arguments)
            self.assertEqual(2, code)
            envelope = self.assert_envelope(stdout, "move_and_patch")
            self.assertEqual("NotFound", envelope["state"])

            with patch.object(MOVE_AND_PATCH, "load_final_index", return_value=index_item), patch.object(MOVE_AND_PATCH, "run_himalaya"), patch.object(MOVE_AND_PATCH, "verify_in_target_folder", return_value="22"), patch.object(MOVE_AND_PATCH, "upsert_final_index_entry", side_effect=OSError("index unavailable")):
                code, stdout, _ = self.invoke_module(MOVE_AND_PATCH, "mail_desk_move_and_patch.py", arguments)
            self.assertEqual(1, code)
            envelope = self.assert_envelope(stdout, "move_and_patch")
            self.assertFalse(envelope["success"])
            self.assertEqual("PartialFailure", envelope["state"])
            self.assertTrue(envelope["data"]["mailbox"]["copy_completed"])
            self.assertTrue(envelope["data"]["mailbox"]["target_verified"])
            self.assertFalse(envelope["data"]["index"]["updated"])
            self.assertEqual("index", envelope["data"]["failure_phase"])

            with patch.object(MOVE_AND_PATCH, "load_final_index", return_value=index_item), patch.object(MOVE_AND_PATCH, "run_himalaya"), patch.object(MOVE_AND_PATCH, "verify_in_target_folder", side_effect=RuntimeError("verification unavailable")), patch.object(MOVE_AND_PATCH, "upsert_final_index_entry") as upsert:
                code, stdout, _ = self.invoke_module(MOVE_AND_PATCH, "mail_desk_move_and_patch.py", arguments)
            self.assertEqual(1, code)
            envelope = self.assert_envelope(stdout, "move_and_patch")
            self.assertEqual("PartialFailure", envelope["state"])
            self.assertEqual("Target verification failed after mailbox copy.", envelope["message"])
            self.assertEqual("verify", envelope["data"]["failure_phase"])
            self.assertEqual("verify", envelope["error"]["details"]["phase"])
            self.assertTrue(envelope["data"]["mailbox"]["copy_completed"])
            self.assertFalse(envelope["data"]["mailbox"]["target_verified"])
            upsert.assert_not_called()

            code, stdout, stderr = self.invoke_module(MOVE_AND_PATCH, "mail_desk_move_and_patch.py", ["--message-id", "missing@example.test"])
            self.assertEqual(2, code)
            envelope = self.assert_envelope(stdout, "move_and_patch")
            self.assertEqual("ArgumentError", envelope["error"]["type"])
            self.assertIn("usage:", stderr)

    def test_himalaya_human_json_argument_and_manifest_contracts(self):
        with patch.object(HIMALAYA_CLIENT, "op_list_folders", return_value=[{"name": "INBOX"}]):
            code, human, stderr = self.invoke_module(HIMALAYA_CLIENT, "mail_desk_himalaya_client.py", ["list-folders"])
        self.assertEqual(0, code, stderr)
        self.assertIn("Total results: 1", human)
        self.assertNotIn('"action"', human)

        with patch.object(HIMALAYA_CLIENT, "op_list_folders", return_value=[{"name": "INBOX"}]):
            code, stdout, stderr = self.invoke_module(HIMALAYA_CLIENT, "mail_desk_himalaya_client.py", ["list-folders", "--json"])
        self.assertEqual(0, code, stderr)
        envelope = self.assert_envelope(stdout, "himalaya_client")
        self.assertTrue(envelope["success"])
        self.assertEqual("list_folders", envelope["data"]["operation"])

        code, stdout, stderr = self.invoke_module(HIMALAYA_CLIENT, "mail_desk_himalaya_client.py", ["--input=missing.json", "--unknown"])
        self.assertEqual(2, code)
        envelope = self.assert_envelope(stdout, "himalaya_client")
        self.assertEqual("ArgumentError", envelope["error"]["type"])
        self.assertIn("usage:", stderr)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "partial.json"
            partial.write_text(
                json.dumps({"operations": [{"action": "list_folders"}, {"action": "delete", "folder": "INBOX", "envelope_id": "7"}], "delete_input_on_success": True}),
                encoding="utf-8",
            )
            with patch.object(HIMALAYA_CLIENT, "op_list_folders", return_value=[{"name": "INBOX"}]), patch.object(HIMALAYA_CLIENT, "op_delete_message", side_effect=RuntimeError("delete failed")):
                code, stdout, _ = self.invoke_module(HIMALAYA_CLIENT, "mail_desk_himalaya_client.py", ["--input", str(partial)])
            self.assertEqual(1, code)
            envelope = self.assert_envelope(stdout, "himalaya_client")
            self.assertEqual("PartialFailure", envelope["state"])
            results = envelope["data"]["results"]
            self.assertEqual([True, False], [result["success"] for result in results])
            self.assertIsNone(results[0]["error"])
            self.assertIsNotNone(results[1]["error"])
            self.assertFalse(envelope["data"]["input_file_deleted"])
            self.assertTrue(partial.exists())

            completed = root / "completed.json"
            completed.write_text(json.dumps({"operations": [{"action": "list_folders"}], "delete_input_on_success": True}), encoding="utf-8")
            with patch.object(HIMALAYA_CLIENT, "op_list_folders", return_value=[{"name": "INBOX"}]):
                code, stdout, _ = self.invoke_module(HIMALAYA_CLIENT, "mail_desk_himalaya_client.py", ["--input", str(completed)])
            self.assertEqual(0, code)
            envelope = self.assert_envelope(stdout, "himalaya_client")
            self.assertTrue(envelope["success"])
            self.assertTrue(envelope["data"]["input_file_deleted"])
            self.assertFalse(completed.exists())


if __name__ == "__main__":
    unittest.main()
