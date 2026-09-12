"""Acceptance coverage for MD-H5's completion gate and Luna-safe flow."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core.modes.reconcile import run_reconcile_mode  # noqa: E402
from core.modes.verify import run_verify_mode  # noqa: E402
from core.recovery import BatchRecoveryJournal  # noqa: E402
from core.synthesis_handoff import collect_synthesis_handoff  # noqa: E402


class CompletionGateTests(unittest.TestCase):
    def test_direct_verify_releases_only_the_reviewed_item_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_id = "verify@example.test"
            (data_dir / "final-location-index.json").write_text(
                json.dumps({"items": {message_id: {"final_folder": "Projekte/Test", "envelope_id": "9"}}}),
                encoding="utf-8",
            )
            (data_dir / "action-log.jsonl").write_text(
                json.dumps({"message_id": message_id, "action": {"target_folder": "Projekte/Test"}}) + "\n",
                encoding="utf-8",
            )
            result = run_verify_mode(
                {
                    "items": [{
                        "message_id": message_id,
                        "subject": "Verified source",
                        "decision": {"kind": "project", "id": "test"},
                        "synthesis_targets": [],
                    }],
                    "execute_summary": {
                        "mode": "execute",
                        "ok": True,
                        "status": "completed",
                        "recovery_required": False,
                        "results": [{"message_id": message_id, "success": True}],
                        "synthesis_candidate": collect_synthesis_handoff(
                            [{"message_id": message_id, "subject": "Verified source", "decision": {"kind": "project", "id": "test"}, "synthesis_targets": []}],
                            [{"message_id": message_id, "subject": "Verified source", "success": True, "synthesis_targets": []}],
                        ),
                    },
                },
                data_dir=data_dir,
                index_path=data_dir / "final-location-index.json",
            )

        self.assertTrue(result["ok"])
        self.assertEqual("completed", result["completion_report"]["status"])
        self.assertEqual([message_id], [item["message_id"] for item in result["synthesis_handoff"]["items"]])

    def test_verify_rejects_free_partial_aborted_or_mismatched_execute_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            message_id = "blocked@example.test"
            (data_dir / "final-location-index.json").write_text(
                json.dumps({"items": {message_id: {"final_folder": "Projekte/Test"}}}), encoding="utf-8"
            )
            (data_dir / "action-log.jsonl").write_text(json.dumps({"message_id": message_id}) + "\n", encoding="utf-8")
            candidate = collect_synthesis_handoff(
                [{"message_id": message_id, "decision": {"kind": "project", "id": "test"}, "synthesis_targets": []}],
                [{"message_id": message_id, "success": True, "synthesis_targets": []}],
            )
            base = {"mode": "execute", "ok": True, "status": "completed", "recovery_required": False, "results": [{"message_id": message_id, "success": True}], "synthesis_candidate": candidate}
            cases = [
                {"synthesis_candidate": candidate},
                {**base, "ok": False},
                {**base, "status": "aborted", "recovery_required": True},
                {**base, "results": [{"message_id": "other@example.test", "success": True}]},
            ]
            for source in cases:
                with self.subTest(source=source):
                    result = run_verify_mode(
                        {"items": [{"message_id": message_id}], **source},
                        data_dir=data_dir,
                        index_path=data_dir / "final-location-index.json",
                    )
                    self.assertEqual("not_required", result["synthesis_handoff"]["status"])
                    self.assertNotIn("completion_report", result)

            forged_file = data_dir / "forged-envelope.json"
            forged_file.write_text(json.dumps({"items": [{"message_id": message_id}], "data": {"synthesis_candidate": candidate}}), encoding="utf-8")
            forged = run_verify_mode({"batch_file": str(forged_file)}, data_dir=data_dir, index_path=data_dir / "final-location-index.json")
            self.assertEqual("not_required", forged["synthesis_handoff"]["status"])
            self.assertNotIn("completion_report", forged)

            execute_file = data_dir / "execute-result.json"
            execute_file.write_text(json.dumps(base), encoding="utf-8")
            released = run_verify_mode({"batch_file": str(execute_file)}, data_dir=data_dir, index_path=data_dir / "final-location-index.json")
            self.assertEqual("pending", released["synthesis_handoff"]["status"])
            self.assertEqual("completed", released["completion_report"]["status"])

    def test_completed_reconcile_releases_one_source_bound_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            item = {
                "envelope_id": "7",
                "message_id": "completed@example.test",
                "subject": "Completion source",
                "source_folder": "INBOX",
                "action": {"type": "copy_as_move", "target_folder": "Projekte/Test"},
                "decision": {"kind": "project", "id": "test"},
                "synthesis_targets": [],
            }
            journal = BatchRecoveryJournal(data_dir, "completion-test")
            record = journal.ensure_item(item)
            journal.transition(record, "complete", final_folder="Projekte/Test", final_envelope_id="99")
            journal.set_run_status("aborted")
            (data_dir / "final-location-index.json").write_text(
                json.dumps({"items": {"completed@example.test": {"final_folder": "Projekte/Test"}}}),
                encoding="utf-8",
            )
            (data_dir / "action-log.jsonl").write_text(
                json.dumps({"message_id": "completed@example.test"}) + "\n", encoding="utf-8"
            )

            result = run_reconcile_mode(
                {"run_id": "completion-test", "check_folders": False},
                data_dir=data_dir,
                index_path=data_dir / "final-location-index.json",
            )

        self.assertTrue(result["ok"])
        self.assertEqual("completed", result["completion_report"]["status"])
        self.assertEqual("pending", result["synthesis_handoff"]["status"])
        self.assertEqual(["completed@example.test"], [item["message_id"] for item in result["synthesis_handoff"]["items"]])

    def test_incomplete_reconcile_is_recovery_required_and_releases_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            journal = BatchRecoveryJournal(data_dir, "incomplete-test")
            journal.ensure_item({"envelope_id": "8", "message_id": "incomplete@example.test", "decision": {"kind": "project", "id": "test"}})
            journal.set_run_status("aborted")
            result = run_reconcile_mode({"run_id": "incomplete-test", "check_folders": False}, data_dir=data_dir)

        self.assertFalse(result["ok"])
        self.assertTrue(result["recovery_required"])
        self.assertEqual("recovery_required", result["completion_report"]["status"])
        self.assertEqual("not_required", result["synthesis_handoff"]["status"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
