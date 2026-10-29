"""Approval boundaries of pipeline and the FR-04b dossier bridge (no live I/O)."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from core.batch_contract import validate_execute_contract
from core.modes.pipeline import run_pipeline_mode
from core.modes.dossier_apply import canonical_execute_request_sha256, run_dossier_apply_mode
from core.modes.execute import run_execute_mode
from review_fixtures import approved_execute


class CallerApprovalTests(unittest.TestCase):
    def test_valid_nested_dossier_approval_reaches_real_execute_and_then_verify(self):
        inner = approved_execute({"mode": "execute", "items": [{
            "envelope_id": "1", "message_id": "case@example.test", "source_folder": "INBOX",
            "action": {"type": "copy_as_move", "target_folder": "Projects/X"},
            "decision": {"kind": "project", "id": "x"},
        }]})
        outer = {"mode": "dossier_apply", "project": "x", "execute_request": inner,
                 "review": {"required": True, "state": "approved", "approval_receipt": {
                     "reviewed_at": "now", "reviewed_by": "human",
                     "execute_request_sha256": canonical_execute_request_sha256(inner),
                 }}}
        mail = Mock()
        verify = Mock(return_value={"ok": True, "results": [{"message_id": "case@example.test", "consistent": True}]})
        def execute(config, **kwargs):
            return run_execute_mode(config, **kwargs, dependencies={
                "run_himalaya": mail, "verify_in_target_folder": Mock(return_value="99"),
                "BatchProgressTracker": Mock(), "auto_resolve_replies_from_sent": Mock(), "sleep": Mock(),
            })
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary) / "data" / "mail-desk"
            result = run_dossier_apply_mode(outer, data_dir=data, dependencies={
                "load_catalogs": Mock(return_value=([{"id": "x", "status": "active", "mailbox_folder": "Projects/X"}], [])),
                "run_execute_mode": execute, "run_verify_mode": verify,
            })
            self.assertTrue((data / "final-location-index.json").is_file())
            self.assertTrue((data / "action-log.jsonl").is_file())
        self.assertTrue(result["ok"])
        self.assertEqual("reviewed", result["execute_summary"]["contract_gate"]["state"])
        self.assertEqual(["copy", "delete"], [call.args[0][1] for call in mail.call_args_list])
        verify.assert_called_once()

    def test_pipeline_only_returns_pending_draft_without_mutating_dependencies(self):
        item = {"envelope_id": "1", "message_id": "case@example.test", "source_folder": "INBOX",
                "action": {"type": "copy_as_move", "target_folder": "Projects/X"},
                "decision": {"confidence": "high"}}
        forbidden = {name: Mock(side_effect=AssertionError(name)) for name in (
            "sync_sent_items", "run_execute_mode", "run_verify_mode",
        )}
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            result = run_pipeline_mode({"count": 1, "sync_sent": True, "verify": True,
                                        "min_confidence": "low", "review": {"state": "approved"}},
                                       account="primary", data_dir=data_dir, dependencies={
                "get_unprocessed_emails": Mock(return_value=([{}], 0)),
                "load_sent_index": Mock(return_value={}),
                "draft_manifest": Mock(return_value={"mode": "execute", "items": [item]}),
                **forbidden,
            })
            self.assertFalse(data_dir.exists())
        for dependency in forbidden.values():
            dependency.assert_not_called()
        self.assertEqual(0, result["executed_count"])
        self.assertEqual("review_required", result["status"])
        self.assertEqual("pending", result["draft"]["review"]["state"])
        self.assertEqual("review_required", validate_execute_contract(result["draft"], effective_account="primary")["reason_code"])

    def test_outer_dossier_receipt_never_authorizes_unbound_inner_execute(self):
        request = {"mode": "execute", "items": [{
            "envelope_id": "1", "message_id": "case@example.test", "source_folder": "INBOX",
            "action": {"type": "copy_as_move", "target_folder": "Projects/X"},
            "decision": {"kind": "project", "id": "x"},
        }]}
        config = {"mode": "dossier_apply", "project": "x", "execute_request": request,
                  "review": {"required": True, "state": "approved", "approval_receipt": {
                      "reviewed_at": "2026-10-28T22:00:00Z", "reviewed_by": "human",
                      "execute_request_sha256": canonical_execute_request_sha256(request),
                  }}}
        execute = Mock()
        verify = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_dossier_apply_mode(config, data_dir=Path(temporary) / "data" / "mail-desk", dependencies={
                "load_catalogs": Mock(return_value=([{"id": "x", "status": "active", "mailbox_folder": "Projects/X"}], [])),
                "run_execute_mode": execute, "run_verify_mode": verify,
            })
        self.assertFalse(result["ok"])
        self.assertEqual("incomplete_contract", result["contract_gate"]["reason_code"])
        execute.assert_not_called()
        verify.assert_not_called()

    def test_dossier_invalid_inner_receipts_stop_before_delegation_even_with_valid_outer(self):
        request = approved_execute({"mode": "execute", "items": [{
            "envelope_id": "1", "message_id": "case@example.test", "source_folder": "INBOX",
            "action": {"type": "copy_as_move", "target_folder": "Projects/X"},
            "decision": {"kind": "project", "id": "x"},
        }]})
        for bad_receipt, reason in ((None, "invalid_review_receipt"), ({}, "invalid_review_receipt"), ({
            "reviewed_at": "now", "reviewed_by": "human", "execute_request_sha256": "0" * 64,
        }, "review_hash_mismatch")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                inner = deepcopy(request)
                inner["review"]["approval_receipt"] = bad_receipt
                config = {"mode": "dossier_apply", "project": "x", "execute_request": inner,
                          "review": {"required": True, "state": "approved", "approval_receipt": {
                              "reviewed_at": "now", "reviewed_by": "human",
                              "execute_request_sha256": canonical_execute_request_sha256(inner),
                          }}}
                execute = Mock()
                verify = Mock()
                result = run_dossier_apply_mode(config, data_dir=Path(temporary) / "data" / "mail-desk", dependencies={
                    "load_catalogs": Mock(return_value=([{"id": "x", "status": "active", "mailbox_folder": "Projects/X"}], [])),
                    "run_execute_mode": execute, "run_verify_mode": verify,
                })
                self.assertEqual(reason, result["contract_gate"]["reason_code"])
                self.assertFalse(result["ok"])
                execute.assert_not_called()
                verify.assert_not_called()
