"""Contract tests for the explicit OI-12 legacy JSON translator."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import legacy_cli_adapter as adapter  # noqa: E402
from core.envelope import MAX_TEXT_LENGTH  # noqa: E402


def canonical(*, action="inspect_manifest", success=True, state="Completed", data=None, error=None):
    return {
        "action": action,
        "success": success,
        "state": state,
        "message": "A canonical result.",
        "data": {} if data is None else data,
        "error": error,
    }


class LegacyCliAdapterTests(unittest.TestCase):
    def invoke(self, arguments: list[str], payload: object = None):
        stdout, stderr = io.StringIO(), io.StringIO()
        stdin = io.StringIO("" if payload is None else json.dumps(payload))
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr), patch.object(sys, "stdin", stdin):
            exit_code = adapter.main(arguments)
        self.assertEqual(1, len(stdout.getvalue().splitlines()), stdout.getvalue())
        self.stderr = stderr.getvalue()
        return exit_code, json.loads(stdout.getvalue())

    def assert_adapter_error(self, result):
        self.assertEqual(adapter.CANONICAL_KEYS, tuple(result))
        self.assertEqual("legacy_cli_adapter", result["action"])
        self.assertFalse(result["success"])
        self.assertEqual("Failed", result["state"])
        self.assertEqual("AdapterInputError", result["error"]["type"])

    def test_inspect_status_profile_unwraps_the_historical_result_payload(self):
        source = canonical(data={"operation": "inspect", "result": {"total_items_in_manifest": 1}})
        code, result = self.invoke(["--profile", "mail-desk-status-v1"], source)

        self.assertEqual(0, code)
        self.assertEqual(
            {"status": "success", "data": {"total_items_in_manifest": 1}, "error": None},
            result,
        )

    def test_himalaya_direct_operation_unwraps_raw_result(self):
        source = canonical(
            action="himalaya_client",
            data={"operation": "list_folders", "result": [{"name": "INBOX"}]},
        )
        code, result = self.invoke(["--profile", "mail-desk-status-v1"], source)

        self.assertEqual(0, code)
        self.assertEqual({"status": "success", "data": [{"name": "INBOX"}], "error": None}, result)

    def test_himalaya_manifest_restores_operation_aware_shape_and_partial_exit(self):
        source = canonical(
            action="himalaya_client",
            success=False,
            state="PartialFailure",
            data={
                "operation": "manifest",
                "total_operations": 2,
                "input_file_deleted": False,
                "results": [
                    {"operation": "list_folders", "success": True, "data": [{"name": "INBOX"}], "error": None},
                    {"operation": "delete", "success": False, "data": {}, "error": {"type": "OperationError", "message": "delete failed"}},
                ],
            },
            error={"type": "PartialFailure", "message": "One operation failed.", "details": {"phase": "delete"}},
        )
        code, result = self.invoke(["--profile", "mail-desk-status-v1"], source)

        self.assertEqual(1, code)
        self.assertEqual("partial", result["status"])
        self.assertEqual(False, result["data"]["all_succeeded"])
        self.assertEqual({"action": "list_folders", "success": True, "result": [{"name": "INBOX"}]}, result["data"]["results"][0])
        self.assertEqual("delete", result["data"]["results"][1]["action"])
        self.assertEqual(source["error"], result["error"])
        self.assertIsNotNone(result["error"])

    def test_batch_runner_restores_mode_and_omits_canonical_operation(self):
        source = canonical(
            action="batch_runner",
            data={"operation": "inspect", "total_processed": 3, "results": []},
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], source)

        self.assertEqual(0, code)
        self.assertEqual({"ok": True, "mode": "inspect", "total_processed": 3, "results": []}, result)
        self.assertNotIn("operation", result)

    def test_final_index_stats_query_and_lookup_restore_historical_flat_shapes(self):
        stats = canonical(
            action="final_location_index",
            data={"operation": "stats", "result": {"total_indexed_items": 4, "folders_count": 2}},
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], stats)
        self.assertEqual(0, code)
        self.assertEqual({"ok": True, "action": "stats", "total_indexed_items": 4, "folders_count": 2}, result)

        query = canonical(
            action="final_location_index",
            data={"operation": "query", "result": {"total_matches": 1, "items": {"m@example.test": {}}}},
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], query)
        self.assertEqual(0, code)
        self.assertEqual("query", result["action"])
        self.assertEqual(1, result["total_matches"])
        self.assertNotIn("operation", result)

        missing = canonical(
            action="final_location_index",
            success=False,
            state="NotFound",
            data={"operation": "lookup", "message_id": "missing@example.test", "item": None},
            error={"type": "OperationError", "message": "Message ID is not indexed."},
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], missing)
        self.assertEqual(1, code)  # The original lookup exit 2 is not in the envelope.
        self.assertEqual({"ok": True, "action": "lookup", "found": False, "message_id": "missing@example.test", "item": None, "error": missing["error"]}, result)

    def test_final_index_manifest_restores_flat_query_and_optional_input_cleanup_field(self):
        manifest = canonical(
            action="final_location_index",
            data={
                "operation": "manifest",
                "total_operations": 2,
                "results": [
                    {
                        "operation": "query",
                        "success": True,
                        "data": {"folder_filter": "Projects/Test", "query_filter": None, "total_matches": 1, "returned_count": 1, "items": {"m@example.test": {}}},
                        "error": None,
                    },
                    {
                        "operation": "upsert",
                        "success": True,
                        "data": {"result": {"message_id": "m@example.test", "updated": True}},
                        "error": None,
                    },
                ],
            },
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], manifest)
        self.assertEqual(0, code)
        self.assertEqual(True, result["ok"])
        self.assertNotIn("input_file_deleted", result)
        self.assertEqual(
            {"ok": True, "action": "query", "folder_filter": "Projects/Test", "query_filter": None, "total_matches": 1, "returned_count": 1, "items": {"m@example.test": {}}},
            result["results"][0],
        )
        self.assertEqual(
            {"ok": True, "action": "upsert", "result": {"message_id": "m@example.test", "updated": True}},
            result["results"][1],
        )

        manifest["data"]["input_file_deleted"] = False
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], manifest)
        self.assertEqual(0, code)
        self.assertIn("input_file_deleted", result)
        self.assertFalse(result["input_file_deleted"])

    def test_preflight_and_move_patch_remove_canonical_operation_and_restore_known_success_fields(self):
        preflight = canonical(
            action="mailbox_preflight",
            data={"operation": "preflight", "skipped": False, "checked": 2, "missing_count": 0, "missing": []},
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], preflight)
        self.assertEqual(0, code)
        self.assertEqual({"ok": True, "skipped": False, "checked": 2, "missing_count": 0, "missing": []}, result)

        move = canonical(
            action="move_and_patch",
            data={
                "operation": "move_and_patch",
                "message_id": "move@example.test",
                "mailbox": {"copy_completed": True, "source_folder": "INBOX", "target_folder": "Projects/Test", "target_envelope_id": "22"},
                "index": {"updated": True},
            },
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], move)
        self.assertEqual(0, code)
        self.assertEqual({"ok": True, "message_id": "move@example.test", "moved": True, "source_folder": "INBOX", "target_folder": "Projects/Test", "new_envelope_id": "22", "index_updated": True}, result)

    def test_unknown_action_profile_pair_fails_closed_with_canonical_adapter_error(self):
        code, result = self.invoke(
            ["--profile", "mail-desk-status-v1"],
            canonical(action="batch_runner"),
        )

        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("does not support action", result["message"])

    def test_flattening_collision_fails_closed_without_overwrite(self):
        code, result = self.invoke(
            ["--profile", "mail-desk-ok-v1"],
            canonical(action="batch_runner", data={"ok": "not a boolean"}),
        )

        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("control key", result["message"])

    def test_fixed_legacy_control_keys_cannot_be_overwritten(self):
        code, result = self.invoke(
            ["--profile", "mail-desk-ok-v1"],
            canonical(action="batch_runner", data={"operation": "inspect", "mode": "execute"}),
        )
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("mode", result["message"])

        code, result = self.invoke(
            ["--profile", "mail-desk-ok-v1"],
            canonical(
                action="final_location_index",
                data={"operation": "stats", "result": {"action": "query", "total_indexed_items": 1}},
            ),
        )
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("action", result["message"])

    def test_exact_canonical_keys_and_types_are_required(self):
        source = canonical(action="batch_runner")
        source["legacy"] = True
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], source)
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("exactly these canonical keys", result["message"])

        invalid_error = canonical(
            action="batch_runner",
            success=False,
            state="Failed",
            error="old string error",
        )
        code, result = self.invoke(["--profile", "mail-desk-ok-v1"], invalid_error)
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("structured error", result["message"])

    def test_duplicate_json_keys_are_rejected_before_translation(self):
        raw = '{"action":"batch_runner","action":"inspect_manifest","success":true,"state":"Completed","message":"x","data":{},"error":null}'
        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout), patch.object(sys, "stdin", io.StringIO(raw)):
            code = adapter.main(["--profile", "mail-desk-ok-v1"])

        self.assertEqual(2, code)
        result = json.loads(stdout.getvalue())
        self.assert_adapter_error(result)
        self.assertIn("duplicate JSON key", result["message"])

    def test_input_file_is_read_only_and_invalid_json_uses_canonical_adapter_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "canonical.json"
            raw = json.dumps(canonical(action="mailbox_preflight", data={"operation": "preflight"}))
            path.write_text(raw, encoding="utf-8")
            code, result = self.invoke(["--profile", "mail-desk-ok-v1", "--input", str(path)])
            self.assertEqual(0, code)
            self.assertTrue(path.exists())
            self.assertEqual(raw, path.read_text(encoding="utf-8"))
            self.assertEqual({"ok": True}, result)

            path.write_text("{", encoding="utf-8")
            code, result = self.invoke(["--profile", "mail-desk-ok-v1", "--input", str(path)])
            self.assertEqual(2, code)
            self.assertTrue(path.exists())
            self.assert_adapter_error(result)
            self.assertIn("not valid JSON", result["message"])

    def test_unknown_profile_is_a_canonical_error_with_usage_on_stderr(self):
        code, result = self.invoke(["--profile", "unknown"], canonical())
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("invalid choice", result["message"])
        self.assertIn("usage:", self.stderr)

        code, result = self.invoke(["--unexpected"], canonical())
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertIn("unrecognized arguments", result["message"])
        self.assertIn("usage:", self.stderr)

    def test_adapter_errors_use_oi10_bounded_envelope(self):
        code, result = self.invoke(
            ["--profile", "mail-desk-status-v1"],
            canonical(action="x" * (MAX_TEXT_LENGTH + 100)),
        )
        self.assertEqual(2, code)
        self.assert_adapter_error(result)
        self.assertEqual(MAX_TEXT_LENGTH, len(result["message"]))
        self.assertEqual(result["message"], result["error"]["message"])
        self.assertTrue(result["message"].endswith("..."))

    def test_help_is_a_canonical_success_with_usage_on_stderr(self):
        code, result = self.invoke(["--help"])
        self.assertEqual(0, code)
        self.assertEqual(adapter.CANONICAL_KEYS, tuple(result))
        self.assertTrue(result["success"])
        self.assertEqual("Completed", result["state"])
        self.assertEqual({"operation": "help"}, result["data"])
        self.assertIsNone(result["error"])
        self.assertIn("usage:", self.stderr)


if __name__ == "__main__":
    unittest.main()
