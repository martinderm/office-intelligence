"""Issue #12/O2 public-seam regressions; hermetic mailbox boundary.

Historical RED/GREEN is preserved in the original recovery lane's evidence.
These tests characterize the transferred O2 delta, not a new TDD cycle.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
from core.modes import reconcile as reconcile_mode  # noqa: E402
from core.recovery import BatchRecoveryJournal  # noqa: E402
from core import himalaya  # noqa: E402

MESSAGE_ID = "9593.issue12@example.test"
SUBJECT = "[mitteilungsblatt] Mitteilungsblatt Nr. 25"


def _seed_journal(data_dir: Path, *, action_type: str, source: str, target: str,
                  envelope_id: str, final_envelope_id: str | None = None) -> None:
    journal = BatchRecoveryJournal(data_dir, "issue12-o2")
    record = journal.ensure_item({
        "envelope_id": envelope_id, "message_id": MESSAGE_ID,
        "source_folder": source, "subject": SUBJECT,
        "from": "sender@example.test", "date": "2026-10-16T08:00:00Z",
        "action": {"type": action_type, "target_folder": target},
        "decision": {}, "synthesis_targets": [],
    })
    if final_envelope_id is None:
        journal.transition(record, "copied")
    else:
        journal.transition(record, "verified", final_envelope_id=final_envelope_id, final_folder=target)


def _seed_complete_local_records(data_dir: Path, *, folder: str, envelope_id: str) -> None:
    (data_dir / "final-location-index.json").write_text(json.dumps({"items": {
        MESSAGE_ID: {"message_id": MESSAGE_ID, "final_folder": folder, "envelope_id": envelope_id},
    }}), encoding="utf-8")
    entry = {"message_id": MESSAGE_ID, "action": {
        "type": "copy_as_move", "target_folder": folder, "new_envelope_id": envelope_id,
    }}
    (data_dir / "action-log.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")


class ReconcileCandidateEnvelopeTests(unittest.TestCase):
    def _data_dir(self, temporary: str) -> Path:
        data_dir = Path(temporary) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        return data_dir

    def test_copy_as_move_cross_folder_does_not_probe_source_envelope_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = self._data_dir(temporary)
            _seed_journal(data_dir, action_type="copy_as_move", source="INBOX",
                          target="Newsletter", envelope_id="9593")
            verify = Mock(return_value=None)
            report = reconcile_mode.run_reconcile_mode(
                {"run_id": "issue12-o2", "check_folders": True}, data_dir=data_dir,
                dependencies={"verify_in_target_folder": verify})
            self.assertEqual("Newsletter", verify.call_args.args[0])
            self.assertIsNone(verify.call_args.kwargs.get("candidate_env_id"),
                              "a source envelope id must not be used as a candidate in another folder")
            row = report["results"][0]
            self.assertFalse(row["folder_verified"])
            self.assertIsNone(row["current_envelope_id"])
            self.assertIsNone(row["candidate_env_id"])
            self.assertEqual("no_candidate_env_id", row["verify_diagnostics"].get("reason"))

    def test_copy_as_move_in_place_keeps_source_envelope_id_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = self._data_dir(temporary)
            _seed_journal(data_dir, action_type="copy_as_move", source="INBOX",
                          target="INBOX", envelope_id="7")
            _seed_complete_local_records(data_dir, folder="INBOX", envelope_id="7")
            def verify(_folder: str, _message_id: str, **kwargs):
                return "7" if kwargs.get("candidate_env_id") == "7" else None
            report = reconcile_mode.run_reconcile_mode(
                {"run_id": "issue12-o2", "check_folders": True}, data_dir=data_dir,
                dependencies={"verify_in_target_folder": verify})
            row = report["results"][0]
            self.assertTrue(row["folder_verified"])
            self.assertEqual("7", row["current_envelope_id"])
            self.assertEqual("7", row["candidate_env_id"])
            self.assertNotIn("reason", row["verify_diagnostics"])
            self.assertEqual("completed", report["status"])

    def test_keep_in_folder_keeps_source_envelope_id_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = self._data_dir(temporary)
            _seed_journal(data_dir, action_type="keep_in_folder", source="INBOX",
                          target="INBOX", envelope_id="7")
            _seed_complete_local_records(data_dir, folder="INBOX", envelope_id="7")
            def verify(_folder: str, _message_id: str, **kwargs):
                return "7" if kwargs.get("candidate_env_id") == "7" else None
            report = reconcile_mode.run_reconcile_mode(
                {"run_id": "issue12-o2", "check_folders": True}, data_dir=data_dir,
                dependencies={"verify_in_target_folder": verify})
            row = report["results"][0]
            self.assertEqual("7", row["candidate_env_id"])
            self.assertNotIn("reason", row["verify_diagnostics"])
            self.assertTrue(row["folder_verified"])
            self.assertEqual("completed", report["status"])

    def test_persisted_final_envelope_id_remains_the_target_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = self._data_dir(temporary)
            _seed_journal(data_dir, action_type="copy_as_move", source="INBOX",
                          target="Newsletter", envelope_id="9593", final_envelope_id="108")
            def verify(_folder: str, _message_id: str, **kwargs):
                return "108" if kwargs.get("candidate_env_id") == "108" else None
            report = reconcile_mode.run_reconcile_mode(
                {"run_id": "issue12-o2", "check_folders": True}, data_dir=data_dir,
                dependencies={"verify_in_target_folder": verify})
            row = report["results"][0]
            self.assertEqual("108", row["candidate_env_id"])
            self.assertNotIn("reason", row["verify_diagnostics"])
            self.assertTrue(row["folder_verified"])
            self.assertEqual("108", row["current_envelope_id"])

    def test_real_verifier_header_diagnostics_reach_report_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = self._data_dir(temporary)
            _seed_journal(data_dir, action_type="copy_as_move", source="INBOX",
                          target="Newsletter", envelope_id="9593")
            before = {p.relative_to(data_dir): p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}
            with patch.object(himalaya, "run_himalaya", side_effect=[
                    json.dumps([{"id": "108", "subject": SUBJECT}]), TimeoutError("synthetic")]):
                report = reconcile_mode.run_reconcile_mode(
                    {"run_id": "issue12-o2", "check_folders": True}, data_dir=data_dir,
                    dependencies={"verify_in_target_folder": himalaya.verify_in_target_folder})
            row = report["results"][0]
            self.assertFalse(row["folder_verified"])
            reads = row["verify_diagnostics"]["header_reads"]
            self.assertEqual(len(reads), 1)
            self.assertEqual(reads[0]["outcome"], "timeout")
            after = {p.relative_to(data_dir): p.read_bytes() for p in data_dir.rglob("*") if p.is_file()}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
