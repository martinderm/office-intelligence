"""Fault-injection coverage for MD-H4's durable execute and reconcile flow."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from core.modes import execute, pipeline, reconcile  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402


def item(*, evidence: bool = False, needs_reply: bool = False) -> dict:
    result = {
        "envelope_id": "7",
        "message_id": "recover@example.test",
        "source_folder": "INBOX",
        "subject": "Recovery subject",
        "from": "sender@example.test",
        "action": {"type": "copy_as_move", "target_folder": "Projekte/Test"},
        "decision": {"needs_reply": needs_reply},
    }
    if evidence:
        result["evidence"] = {
            "file": "memory/evidence/projects/test/2026-09.md",
            "entry": "- recovery evidence recover@example.test",
        }
    return result


class RecoveryJournalTests(unittest.TestCase):
    def _deps(self, run_mail, verify, fault=None):
        deps = {
            "BatchProgressTracker": Mock(),
            "run_himalaya": run_mail,
            "verify_in_target_folder": verify,
            "save_final_index_atomic": Mock(),
            "auto_resolve_replies_from_sent": Mock(),
            "sleep": Mock(),
        }
        if fault:
            deps["fault_inject"] = fault
        return deps

    def test_timeout_after_copy_is_aborted_and_retry_does_not_copy_again(self):
        def fault(phase, _item):
            if phase == "after_copy":
                raise TimeoutError("test timeout")

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            mail = Mock()
            first = execute.run_execute_mode({"items": [item()]}, data_dir=data, dependencies=self._deps(mail, Mock(return_value="99"), fault))
            self.assertEqual("aborted", first["status"])
            self.assertTrue(first["recovery_required"])
            self.assertEqual(1, sum(1 for call in mail.call_args_list if "copy" in call.args[0]))

            report = reconcile.run_reconcile_mode({"check_folders": True}, data_dir=data, dependencies={"verify_in_target_folder": Mock(return_value="99")})
            self.assertTrue(report["read_only"])
            self.assertEqual("needs_local_repair", report["results"][0]["recovery_state"])

            second = execute.run_execute_mode({"items": [item()]}, data_dir=data, dependencies=self._deps(mail, Mock(return_value="99")))
            self.assertTrue(second["ok"])
            self.assertEqual(1, sum(1 for call in mail.call_args_list if "copy" in call.args[0]))
            self.assertEqual(1, sum(1 for call in mail.call_args_list if "delete" in call.args[0]))

    def test_approved_reconcile_repairs_after_delete_once_without_mailbox_mutation(self):
        def fault(phase, _item):
            if phase == "after_delete":
                raise TimeoutError("stop after delete")

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data" / "mail-desk"
            data.mkdir(parents=True)
            mail = Mock()
            execute.run_execute_mode({"items": [item(evidence=True)]}, data_dir=data, dependencies=self._deps(mail, Mock(return_value="99"), fault))
            verify = Mock(return_value="99")
            config = {"check_folders": True, "apply_local_repairs": True, "approval": {"state": "approved"}}
            first = reconcile.run_reconcile_mode(config, data_dir=data, dependencies={"verify_in_target_folder": verify})
            self.assertTrue(first["ok"])
            self.assertGreaterEqual(first["repaired_count"], 1)
            second = reconcile.run_reconcile_mode(config, data_dir=data, dependencies={"verify_in_target_folder": verify})
            self.assertTrue(second["ok"])
            self.assertEqual(0, second["repaired_count"])
            self.assertEqual(1, sum(1 for call in mail.call_args_list if "copy" in call.args[0]))
            self.assertEqual(1, sum(1 for call in mail.call_args_list if "delete" in call.args[0]))
            log_lines = (data / "action-log.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(log_lines))
            evidence = (Path(tmp) / "memory" / "evidence" / "projects" / "test" / "2026-09.md").read_text(encoding="utf-8")
            self.assertEqual(1, evidence.count("recover@example.test"))

    def test_faults_after_verify_index_and_log_are_recoverable_without_duplicate_move(self):
        for phase in ("after_verify", "after_index", "after_log"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp) / "data" / "mail-desk"
                data.mkdir(parents=True)
                mail = Mock()

                def fault(actual, _item):
                    if actual == phase:
                        raise TimeoutError(phase)

                result = execute.run_execute_mode({"items": [item()]}, data_dir=data, dependencies=self._deps(mail, Mock(return_value="99"), fault))
                self.assertEqual("aborted", result["status"])
                report = reconcile.run_reconcile_mode({"check_folders": True}, data_dir=data, dependencies={"verify_in_target_folder": Mock(return_value="99")})
                self.assertIn(report["results"][0]["recovery_state"], {"complete", "needs_local_repair"})
                self.assertEqual(1, sum(1 for call in mail.call_args_list if "copy" in call.args[0]))

    def test_resume_after_each_milestone_fault_never_repeats_side_effects(self):
        for phase in ("after_copy", "after_verify", "after_delete", "after_index", "after_log", "after_reply"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                data = Path(tmp) / "data" / "mail-desk"
                data.mkdir(parents=True)
                mail = Mock()

                def fault(actual, _item):
                    if actual == phase:
                        raise TimeoutError(phase)

                def durable_deps(inject=None):
                    result = {
                        "BatchProgressTracker": Mock(),
                        "run_himalaya": mail,
                        "verify_in_target_folder": Mock(return_value="99"),
                        "auto_resolve_replies_from_sent": Mock(),
                        "sleep": Mock(),
                    }
                    if inject is not None:
                        result["fault_inject"] = inject
                    return result

                payload = {"items": [item(evidence=True, needs_reply=True)]}
                first = execute.run_execute_mode(
                    payload,
                    data_dir=data,
                    dependencies=durable_deps(fault),
                )
                self.assertEqual("aborted", first["status"])
                run_id = first["recovery_journal"]["run_id"]
                second = execute.run_execute_mode(
                    payload,
                    data_dir=data,
                    dependencies=durable_deps(),
                )
                self.assertTrue(second["ok"])
                self.assertEqual("completed", second["status"])
                self.assertEqual("completed", second["recovery_journal"]["status"])
                self.assertEqual(1, sum(1 for call in mail.call_args_list if "copy" in call.args[0]))
                self.assertEqual(1, sum(1 for call in mail.call_args_list if "delete" in call.args[0]))
                self.assertEqual(1, len((data / "action-log.jsonl").read_text(encoding="utf-8").splitlines()))
                self.assertEqual(1, len((data / "replies-needed.jsonl").read_text(encoding="utf-8").splitlines()))
                evidence_path = Path(tmp) / "memory" / "evidence" / "projects" / "test" / "2026-09.md"
                self.assertEqual(1, evidence_path.read_text(encoding="utf-8").count("recover@example.test"))
                journal = json.loads((data / "batch-recovery-journal.json").read_text(encoding="utf-8"))
                run = journal["runs"][run_id]
                record = run["items"]["recover@example.test"]
                phases = [entry["phase"] for entry in record["phases"]]
                self.assertIn("aborted", phases)
                self.assertEqual("complete", record["phase"])
                for milestone in ("copied", "verified", "source_deleted", "indexed", "logged", "evidenced", "complete"):
                    self.assertIn(milestone, phases)

    def test_pipeline_does_not_verify_or_claim_synthesis_after_abort(self):
        verify = Mock()
        executable = item()
        executable["decision"] = {"confidence": "high"}
        result = pipeline.run_pipeline_mode(
            {"sync_sent": False},
            data_dir=Path(tempfile.gettempdir()) / "mail-desk-h4-pipeline",
            dependencies={
                "get_unprocessed_emails": Mock(return_value=([{"envelope_id": "7"}], 0)),
                "draft_manifest": Mock(return_value={"items": [executable]}),
                "run_execute_mode": Mock(return_value={"ok": False, "status": "aborted", "recovery_required": True}),
                "run_verify_mode": verify,
            },
        )
        self.assertEqual("aborted", result["status"])
        self.assertTrue(result["recovery_required"])
        verify.assert_not_called()

    def test_cli_exposes_read_only_reconcile_without_competing_mode(self):
        args = runner._build_parser().parse_args(["--reconcile"])
        config = runner._direct_mode_config(args, Path(tempfile.gettempdir()) / "mail-desk-h4-cli")
        self.assertEqual("reconcile", config["mode"])
        self.assertFalse(config["apply_local_repairs"])
        args = runner._build_parser().parse_args(["--reconcile", "--inspect", "1"])
        with self.assertRaisesRegex(runner.ArgumentParseError, "cannot be combined"):
            runner._direct_mode_config(args, Path(tempfile.gettempdir()) / "mail-desk-h4-cli")


if __name__ == "__main__":
    unittest.main()
