"""FR-19/MD-RC1 reconcile stale-repair tests.

Hermetic contract tests. They exercise the real reconcile mode against a
temporarily materialized recovery journal (built through the real execute mode
with an injected fault, mirroring the Env 9428 finding); no mailbox, network
or catalog access happens.

Red-Gate history: at the MD-RC1-001 dispatch ``apply_local_repairs`` repaired
only *missing* index/log/evidence records (``if not in_index`` / ``if not
in_log``) and the repair flow never touched ``runner-progress.json``; the
stale-record and tracker tests below failed then and pin the resolved
behavior since.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes import execute as execute_mode  # noqa: E402
from core.modes import reconcile as reconcile_mode  # noqa: E402


def _item() -> dict:
    return {
        "message_id": "1954756627.1848513.1785929416691@mail.yahoo.com",
        "envelope_id": "9428",
        "subject": "Re: Antw: THE CALL DOCUMENT",
        "from": "sender@example.test",
        "date": "2026-09-23T10:00:00Z",
        "decision": {"action": "move", "target_folder": "Projekte/In Ausarbeitung/ATAEL"},
    }


def _execute_deps(mail: Mock, verify: Mock, fault=None) -> dict:
    return {
        "run_himalaya": mail,
        "verify_in_target_folder": verify,
        "inject_fault": fault,
        "BatchProgressTracker": Mock(),
    }


class StaleRecordRepairTests(unittest.TestCase):
    """Env 9428 case: record exists but verified target differs from record."""

    def test_stale_index_and_log_records_are_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            # First run: executes as move but times out after copy -> journal
            # carries the item as interrupted routing.
            mail = Mock()
            verify = Mock(return_value="82")
            execute_mode.run_execute_mode(
                {"items": [_item()]},
                data_dir=data,
                dependencies=_execute_deps(mail, verify, lambda phase, _item: "after_copy" if phase == "after_copy" else None),
            )
            # Seed STALE records: index + log point to INBOX / 9428 (Env 9428 state).
            index_path = data / "final-location-index.json"
            index_data = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"items": {}}
            message_id = _item()["message_id"]
            index_data["items"][message_id] = {
                "message_id": message_id,
                "backend": "himalaya",
                "final_folder": "INBOX",
                "envelope_id": "9428",
                "in_reply_to": "",
                "references": [],
                "subject": _item()["subject"],
                "from": _item()["from"],
                "date": _item()["date"],
                "updated_at": "2026-09-23T10:00:00Z",
            }
            index_path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log_entry = {
                "timestamp": "2026-09-23T10:00:00Z",
                "envelope_id": "9428",
                "message_id": message_id,
                "subject": _item()["subject"],
                "from": _item()["from"],
                "action": {"type": "copy_as_move", "source_folder": "INBOX", "target_folder": "INBOX", "new_envelope_id": "9428"},
                "decision": {},
                "notes": "",
            }
            (data / "action-log.jsonl").write_text(json.dumps(log_entry, ensure_ascii=False) + "\n", encoding="utf-8")

            config = {"check_folders": True, "apply_local_repairs": True, "approval": {"state": "approved"}}
            report = reconcile_mode.run_reconcile_mode(
                config,
                data_dir=data,
                index_path=index_path,
                dependencies={"verify_in_target_folder": verify},
            )

            repaired = report["results"][0]["repaired"]
            self.assertIn("index", repaired, f"stale index record must be repaired, got {repaired}")
            self.assertIn("action_log", repaired, f"stale action log must get reconciled entry, got {repaired}")

            corrected = json.loads(index_path.read_text(encoding="utf-8"))["items"][message_id]
            self.assertEqual("Projekte/In Ausarbeitung/ATAEL", corrected["final_folder"])
            self.assertEqual("82", corrected["envelope_id"])
            self.assertNotEqual("2026-09-23T10:00:00Z", corrected["updated_at"])

            log_lines = (data / "action-log.jsonl").read_text(encoding="utf-8").splitlines()
            entries = [json.loads(line) for line in log_lines if line.strip()]
            self.assertEqual(2, len(entries), "original log entry stays; one reconciled entry appended")
            reconciled = entries[-1]
            self.assertTrue(reconciled.get("reconciled"))
            self.assertEqual("Projekte/In Ausarbeitung/ATAEL", reconciled["action"]["target_folder"])
            self.assertEqual("82", reconciled["action"]["new_envelope_id"])
            # Original entry untouched
            self.assertEqual("INBOX", entries[0]["action"]["target_folder"])

    def test_second_repair_run_without_drift_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            mail = Mock()
            verify = Mock(return_value="82")
            execute_mode.run_execute_mode(
                {"items": [_item()]},
                data_dir=data,
                dependencies=_execute_deps(mail, verify, lambda phase, _item: "after_copy" if phase == "after_copy" else None),
            )
            message_id = _item()["message_id"]
            index_path = data / "final-location-index.json"
            index_data = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"items": {}}
            index_data["items"][message_id] = {
                "message_id": message_id,
                "backend": "himalaya",
                "final_folder": "Projekte/In Ausarbeitung/ATAEL",
                "envelope_id": "82",
                "in_reply_to": "",
                "references": [],
                "subject": _item()["subject"],
                "from": _item()["from"],
                "date": _item()["date"],
                "updated_at": "2026-09-23T10:00:00Z",
            }
            index_path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log_entry = {
                "timestamp": "2026-09-23T10:00:00Z",
                "envelope_id": "82",
                "message_id": message_id,
                "subject": _item()["subject"],
                "from": _item()["from"],
                "action": {"type": "copy_as_move", "source_folder": "INBOX", "target_folder": "Projekte/In Ausarbeitung/ATAEL", "new_envelope_id": "82"},
                "decision": {},
                "notes": "",
            }
            (data / "action-log.jsonl").write_text(json.dumps(log_entry) + "\n", encoding="utf-8")

            config = {"check_folders": True, "apply_local_repairs": True, "approval": {"state": "approved"}}
            first = reconcile_mode.run_reconcile_mode(config, data_dir=data, index_path=index_path, dependencies={"verify_in_target_folder": verify})
            self.assertEqual(0, first["repaired_count"], "no drift -> no repair")

    def test_no_repair_without_verification_and_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            mail = Mock()
            execute_mode.run_execute_mode(
                {"items": [_item()]},
                data_dir=data,
                dependencies=_execute_deps(mail, Mock(return_value="82"), lambda phase, _item: "after_copy" if phase == "after_copy" else None),
            )
            message_id = _item()["message_id"]
            index_path = data / "final-location-index.json"
            index_data = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {"items": {}}
            index_data["items"][message_id] = {
                "message_id": message_id, "backend": "himalaya", "final_folder": "INBOX",
                "envelope_id": "9428", "in_reply_to": "", "references": [],
                "subject": _item()["subject"], "from": _item()["from"], "date": _item()["date"],
                "updated_at": "2026-09-23T10:00:00Z",
            }
            index_path.write_text(json.dumps(index_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            stale_before = index_path.read_text(encoding="utf-8")
            no_approval = reconcile_mode.run_reconcile_mode(
                {"check_folders": True, "apply_local_repairs": True},
                data_dir=data, index_path=index_path, dependencies={"verify_in_target_folder": Mock(return_value="82")},
            )
            self.assertEqual("approval_required", no_approval["status"])
            self.assertTrue(no_approval["read_only"])

            approved_no_verify = reconcile_mode.run_reconcile_mode(
                {"check_folders": False, "apply_local_repairs": True, "approval": {"state": "approved"}},
                data_dir=data, index_path=index_path, dependencies={},
            )
            self.assertEqual("verification_required", approved_no_verify["status"])
            self.assertTrue(approved_no_verify["read_only"])
            self.assertEqual(json.loads(stale_before), json.loads(index_path.read_text(encoding="utf-8")), "no write without verification")


class RunnerProgressTrackerTests(unittest.TestCase):
    """Micro-FR: the repair flow updates runner-progress.json deterministically."""

    def test_progress_file_followed_up_after_successful_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            # Simulate the Env 9428 end state: the interrupted execute run left
            # the tracker on "failed".
            progress_file = data / "runner-progress.json"
            progress_file.write_text(
                json.dumps({"schema_version": 1, "run_id": "execute_20260923_100000", "mode": "execute", "status": "failed", "error": "Target verification failed after copy."}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            mail = Mock()
            verify = Mock(return_value="82")
            execute_mode.run_execute_mode(
                {"items": [_item()]},
                data_dir=data,
                dependencies=_execute_deps(mail, verify, lambda phase, _item: "after_copy" if phase == "after_copy" else None),
            )
            config = {"check_folders": True, "apply_local_repairs": True, "approval": {"state": "approved"}}
            report = reconcile_mode.run_reconcile_mode(config, data_dir=data, dependencies={"verify_in_target_folder": verify})
            self.assertTrue(report["ok"])
            state = json.loads(progress_file.read_text(encoding="utf-8"))
            self.assertIn(state["status"], {"completed", "repaired"}, f"tracker must be followed up deterministically, got {state['status']!r}")
            self.assertNotEqual("failed", state["status"])

    def test_no_progress_write_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            mail = Mock()
            verify = Mock(return_value="82")
            execute_mode.run_execute_mode(
                {"items": [_item()]},
                data_dir=data,
                dependencies=_execute_deps(mail, verify, lambda phase, _item: "after_copy" if phase == "after_copy" else None),
            )
            config = {"check_folders": True, "apply_local_repairs": True, "approval": {"state": "approved"}}
            report = reconcile_mode.run_reconcile_mode(config, data_dir=data, dependencies={"verify_in_target_folder": verify})
            self.assertTrue(report["ok"])
            self.assertFalse((data / "runner-progress.json").exists(), "no progress file must be invented from nothing")

    def test_progress_file_unchanged_for_read_only_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            progress_file = data / "runner-progress.json"
            original = {"schema_version": 1, "run_id": "execute_20260923_100000", "mode": "execute", "status": "failed", "error": "boom"}
            progress_file.write_text(json.dumps(original) + "\n", encoding="utf-8")
            mail = Mock()
            execute_mode.run_execute_mode(
                {"items": [_item()]},
                data_dir=data,
                dependencies=_execute_deps(mail, Mock(return_value="82"), lambda phase, _item: "after_copy" if phase == "after_copy" else None),
            )
            reconcile_mode.run_reconcile_mode({"check_folders": True}, data_dir=data, dependencies={"verify_in_target_folder": Mock(return_value="82")})
            self.assertEqual(original, json.loads(progress_file.read_text(encoding="utf-8")), "read-only reconcile never touches the tracker")


if __name__ == "__main__":
    unittest.main()