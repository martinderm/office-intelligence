"""Event preparation is read-only and composes canonical mail-desk requests."""
from __future__ import annotations
import copy
import importlib
import sys
import tempfile
import json
import subprocess
from unittest.mock import patch
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core.modes.dossier_synthesis import canonical_json_sha256
from core.batch_contract import add_draft_contract, canonical_execute_request_sha256 as batch_hash
from core.modes.dossier_apply import canonical_execute_request_sha256 as dossier_hash

class EventDossierTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module("mail_desk_event_dossier")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_stage(self, stage, **values):
        result = self.api.process_request({"schema_version": 1, "stage": stage, **values}, workspace_root=self.root)
        self.assertIsInstance(result, dict)
        return result

    def scope(self):
        return {"account": "synthetic", "folders": ["INBOX", "Archive"], "terms": ["workshop"],
                "scan_limit_per_folder": 20, "inspect_limit": 5}

    def source(self, folder="INBOX", eid="1", mid="source@example.test", sha="a" * 64):
        return {"search": {"account": "synthetic", "folder": folder, "envelope_id": eid, "message_id": mid},
                "read": {"account": "synthetic", "folder": folder, "envelope_id": eid,
                         "message_id": mid, "content_sha256": "b" * 64, "references": [], "in_reply_to": None},
                "attachments": [{"account": "synthetic", "folder": folder, "envelope_id": eid,
                    "message_id": mid, "filename": "program.pdf", "original_filename": "program.pdf",
                    "mime_type": "application/pdf", "size_bytes": 20, "sha256": sha, "part_locator": "2",
                    "fetch_status": "available", "provenance": "rfc822_mime", "policy_status": "allowed"}],
                "calendar_objects": []}

    def operative(self):
        item = {"envelope_id": "1", "source_folder": "INBOX", "message_id": "source@example.test",
                "subject": "Synthetic workshop", "action": {"type": "copy_as_move", "target_folder": "Projects/demo"},
                "decision": {"kind": "project", "id": "demo", "confidence": "high", "needs_reply": False}}
        execute = add_draft_contract({"mode": "execute", "account": "synthetic", "items": [item]},
            expected_count=1, allow_fewer=False, source_folder="INBOX", account="synthetic", skip_known=False)
        return {"mode": "dossier_apply", "project": "demo", "account": "synthetic",
                "execute_request": execute, "delete_input_on_success": False,
                "review": {"required": True, "state": "pending"}}

    def review(self, sources=None, operative=None):
        return self.run_stage("review", scope=self.scope(), sources=sources or [self.source()],
                              operative_request=operative or self.operative(), attachment_requests=[])

    def test_plan_is_explicit_bounded_and_never_claims_completeness(self):
        result = self.run_stage("plan", scope=self.scope())
        self.assertTrue(result["ok"])
        self.assertEqual(["INBOX", "Archive"], [r["folder"] for r in result["search_requests"]])
        self.assertTrue(all(r["mode"] == "search" for r in result["search_requests"]))
        self.assertFalse(result["completeness"]["exhaustive"])
        self.assertEqual("first_page_envelopes", result["completeness"]["scan_basis"])
        self.assertEqual("pending_workspace_revalidation", result["account_binding"])
        self.assertEqual([], list(self.root.iterdir()))

    def test_plan_rejects_implicit_scope_bool_limits_and_oversized_inspection(self):
        for changes in ({"folders": []}, {"folders": ["INBOX", "INBOX"]}, {"terms": []},
                        {"scan_limit_per_folder": True}, {"inspect_limit": 0}):
            with self.subTest(changes=changes):
                self.assertFalse(self.run_stage("plan", scope={**self.scope(), **changes})["ok"])
        result = self.run_stage("plan", scope=self.scope(), inspect_ids=[str(i) for i in range(6)])
        self.assertFalse(result["ok"])

    def test_review_dedupes_message_and_attachment_hashes_retaining_locations(self):
        result = self.review([self.source(), self.source("Archive", "9", "<SOURCE@example.test>")])
        self.assertTrue(result["ok"])
        self.assertEqual(1, len(result["sources"]))
        self.assertEqual(2, len(result["sources"][0]["locations"]))
        self.assertEqual(1, len(result["attachments"]))
        self.assertEqual(2, len(result["attachments"][0]["provenance"]))
        self.assertEqual("pending", result["operative_request"]["review"]["state"])
        self.assertNotIn("approval_receipt", result["operative_request"]["review"])

    def test_review_rejects_locator_account_message_and_attachment_drift(self):
        for section, field, value in (("read", "account", "other"), ("read", "message_id", "other@example.test"),
                                     ("read", "envelope_id", "9"), ("attachment", "message_id", "other@example.test"),
                                     ("attachment", "sha256", "invalid")):
            source = self.source()
            target = source["attachments"][0] if section == "attachment" else source[section]
            target[field] = value
            with self.subTest(section=section, field=field):
                self.assertFalse(self.review([source])["ok"])

    def test_calendar_conflicts_and_missing_thread_parents_require_semantic_review(self):
        first, second = self.source(), self.source("Archive", "9")
        first["calendar_objects"] = [{"uid": "calendar-a", "sequence": 1, "recurrence_id": None, "content_sha256": "c" * 64}]
        second["calendar_objects"] = [{"uid": "calendar-b", "sequence": 2, "recurrence_id": None, "content_sha256": "d" * 64}]
        first["read"]["in_reply_to"] = "missing@example.test"
        result = self.review([first, second])
        self.assertTrue(result["review_required"])
        self.assertTrue(result["calendar_conflicts"])
        self.assertEqual(["missing@example.test"], result["missing_thread_parents"])

    def test_same_message_identity_different_content_is_not_silently_merged(self):
        first, second = self.source(), self.source("Archive", "9")
        second["read"]["content_sha256"] = "e" * 64
        self.assertFalse(self.review([first, second])["ok"])

    def test_nested_review_hashes_are_ordered_and_bind_exact_final_inner_receipt(self):
        operative = self.operative()
        initial = self.review(operative=operative)
        self.assertEqual(batch_hash(operative["execute_request"]), initial["review_binding"]["inner_sha256"])
        self.assertEqual(dossier_hash(operative["execute_request"]), initial["review_binding"]["outer_sha256"])
        approved = copy.deepcopy(operative)
        approved["execute_request"]["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:00:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": batch_hash(approved["execute_request"])}}
        frozen = self.review(operative=approved)
        self.assertEqual(initial["review_binding"]["inner_sha256"], frozen["review_binding"]["inner_sha256"])
        self.assertNotEqual(initial["review_binding"]["outer_sha256"], frozen["review_binding"]["outer_sha256"])
        approved["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:01:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": frozen["review_binding"]["outer_sha256"]}}
        self.assertTrue(self.review(operative=approved)["ok"])
        approved["execute_request"]["items"][0]["action"]["target_folder"] = "Other"
        self.assertFalse(self.review(operative=approved)["ok"])

    def test_handoff_rejects_partial_apply_and_report_rejects_missing_artifacts(self):
        result = self.run_stage("handoff", scope=self.scope(), apply_request=self.operative(),
                                apply_result={"ok": False, "results": []}, event={"id": "workshop"})
        self.assertFalse(result["ok"])
        self.assertFalse(self.run_stage("report", artifacts=["memory/references/projects/demo/events/workshop/index.md"])["ok"])

    def test_unknown_fields_and_stages_fail_closed(self):
        self.assertFalse(self.run_stage("plan", scope=self.scope(), execute=True)["ok"])
        self.assertFalse(self.run_stage("execute", scope=self.scope())["ok"])

    def test_repeat_review_is_deterministic_and_never_changes_operative_request(self):
        request = self.operative()
        before = copy.deepcopy(request)
        self.assertEqual(self.review(operative=request), self.review(operative=request))
        self.assertEqual(before, request)
        self.assertEqual([], list(self.root.iterdir()))

    def test_completed_apply_handoff_is_source_bound_and_report_verifies_outputs(self):
        operative = self.operative()
        execute = operative["execute_request"]
        execute["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:00:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": batch_hash(execute)}}
        receipt = {"reviewed_at": "2026-01-01T12:01:00Z", "reviewed_by": "human-fixture",
                   "execute_request_sha256": dossier_hash(execute)}
        operative["review"] = {"required": True, "state": "approved", "approval_receipt": receipt}
        applied = {"ok": True, "mode": "dossier_apply", "project": "demo", "approval_receipt": receipt,
            "review": {"required": True, "state": "completed", "approval_receipt": receipt},
            "execute_summary": {"ok": True, "mode": "execute", "results": [{"success": True,
                "message_id": "source@example.test", "synthesis_targets": []}]},
            "verify_summary": {"ok": True, "mode": "verify", "results": [{"consistent": True,
                "message_id": "source@example.test"}]},
            "synthesis_handoff": {"schema_version": 1, "status": "pending", "items": [{
                "message_id": "source@example.test", "subject": "Synthetic workshop", "kind": "project",
                "id": "demo", "synthesis_targets": [], "target_selection_required": True}]}}
        handoff = self.run_stage("handoff", scope=self.scope(), apply_request=operative,
            apply_result=applied, apply_result_sha256=canonical_json_sha256(applied), sources=[self.source()],
            event={"owner_kind": "project", "owner_id": "demo", "id": "workshop", "title": "Synthetic workshop", "start": "2026-01-01"})
        self.assertTrue(handoff["ok"])
        self.assertEqual("event-documentation", handoff["work_order"]["owner"])
        self.assertEqual("source@example.test", handoff["work_order"]["sources"][0]["message_id"])
        self.assertEqual("pending_content_review", handoff["work_order"]["status"])
        self.assertFalse(handoff["completeness"]["exhaustive"])
        from core.quarantine.attachment_fetch import _load_workspace_lock_guard
        guard = _load_workspace_lock_guard()
        guard.acquire_workspace_lock(self.root, harness="synthetic-test", lease_id="synthetic-handoff-writer")
        catalog = self.root / "memory/references/projects/projects.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"projects": [{"id": "demo", "title": "Demo", "status": "active",
            "mailbox_folder": "Projects/demo"}]}), encoding="utf-8")
        writer_scripts = Path(__file__).resolve().parents[3] / "skills/event-documentation/scripts"
        sys.path.insert(0, str(writer_scripts))
        writer = importlib.import_module("event_documentation")
        source_id = handoff["work_order"]["sources"][0]["id"]
        written = writer.apply_event({"schema_version": 1, "work_order": handoff["work_order"],
            "content": {"overview": [{"text": "Synthetic workshop", "source_ids": [source_id]}],
                "planned_agenda": [], "observed_decisions": [], "reply_triage": [], "todo_triage": []}},
            workspace_root=self.root, lease_id="synthetic-handoff-writer")
        self.assertTrue(written["ok"])
        self.assertEqual("human_content_review_required", written["content_review"])
        artifact = "memory/references/projects/demo/events/workshop/index.md"
        target = self.root / artifact
        self.assertTrue(target.is_file())
        report = self.run_stage("report", artifacts=[artifact])
        self.assertTrue(report["ok"])
        self.assertEqual([artifact], report["verified_outputs"])
        self.assertFalse(report["content_approved"])
        applied["verify_summary"]["results"][0]["message_id"] = "other@example.test"
        self.assertFalse(self.run_stage("handoff", scope=self.scope(), apply_request=operative,
            apply_result=applied, apply_result_sha256=canonical_json_sha256(applied), sources=[self.source()],
            event={"owner_kind": "project", "owner_id": "demo", "id": "workshop", "title": "Synthetic workshop", "start": "2026-01-01"})["ok"])

    def test_cli_invalid_json_and_parser_errors_return_bounded_json(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "mail_desk_event_dossier.py"
        request = self.root / "request.json"
        for data in ('{"stage":"plan","stage":"review"}', '{"schema_version":NaN}', '{}'):
            request.write_text(data, encoding="utf-8")
            run = subprocess.run([sys.executable, "-B", str(script), "--request", str(request),
                "--workspace", str(self.root), "--json"], capture_output=True, text=True)
            self.assertNotEqual(0, run.returncode)
            envelope = json.loads(run.stdout)
            self.assertFalse(envelope["success"])
            self.assertNotIn(str(self.root), run.stdout)
            self.assertNotIn("Traceback", run.stdout + run.stderr)
        run = subprocess.run([sys.executable, "-B", str(script), "--invalid", "--json"], capture_output=True, text=True)
        self.assertNotEqual(0, run.returncode)
        self.assertFalse(json.loads(run.stdout)["success"])
