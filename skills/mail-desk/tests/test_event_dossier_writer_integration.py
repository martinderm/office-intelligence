"""Genuine writer <-> mail-desk event-dossier facade integration (#14).

The project case runs the real facade ``handoff`` -> real writer ``apply_event``
-> real facade ``report`` (the operative mailbox apply itself stays an external
synthetic fixture; no live mailbox is touched).  The topic case runs the real
writer on a topic work order and the extended report; no topic handoff is
invented because the facade deliberately composes only project work orders.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[3]
SCRIPTS = BUNDLE / "skills" / "mail-desk" / "scripts"
WRITER = BUNDLE / "skills" / "event-documentation" / "scripts" / "event_documentation.py"
LOCK_ROOT = Path(
    os.environ.get("EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT") or (BUNDLE.parent / "workspace-lock")
)
LOCK_CLI = LOCK_ROOT / "scripts" / "invoke_workspace_lock.py"

sys.path.insert(0, str(SCRIPTS))
from core.batch_contract import add_draft_contract, canonical_execute_request_sha256 as batch_hash  # noqa: E402
from core.event_dossier import _completeness  # noqa: E402
from core.modes.dossier_apply import canonical_execute_request_sha256 as dossier_hash  # noqa: E402
from core.modes.dossier_synthesis import canonical_json_sha256  # noqa: E402


def load_writer():
    spec = importlib.util.spec_from_file_location("event_documentation_integration", WRITER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EventDossierWriterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.facade = importlib.import_module("mail_desk_event_dossier")
        self.writer = load_writer()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lease = "synthetic-event-writer-integration"
        (self.root / "memory/references/projects").mkdir(parents=True)
        (self.root / "memory/references/projects/projects.json").write_text(
            json.dumps({"projects": [{"id": "demo", "title": "Demo", "status": "active",
                                      "mailbox_folder": "Projects/demo"}]}), encoding="utf-8")
        (self.root / "memory/references/topics").mkdir(parents=True)
        (self.root / "memory/references/topics/topics.json").write_text(
            json.dumps({"topics": [self.topic_entry()]}), encoding="utf-8")
        run = subprocess.run([sys.executable, "-B", str(LOCK_CLI), "acquire", str(self.root),
                              "--harness", "synthetic-test", "--lease-id", self.lease,
                              "--duration-minutes", "10", "--json"], capture_output=True, text=True)
        self.assertEqual(0, run.returncode, run.stderr)

    # --- facade fixtures (external synthetic operative apply) -----------------
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
                                     expected_count=1, allow_fewer=False, source_folder="INBOX",
                                     account="synthetic", skip_known=False)
        return {"mode": "dossier_apply", "project": "demo", "account": "synthetic",
                "execute_request": execute, "delete_input_on_success": False,
                "review": {"required": True, "state": "pending"}}

    def approved(self):
        operative = self.operative()
        execute = operative["execute_request"]
        execute["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:00:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": batch_hash(execute)}}
        operative["review"] = {"required": True, "state": "approved", "approval_receipt": {
            "reviewed_at": "2026-01-01T12:01:00Z", "reviewed_by": "human-fixture",
            "execute_request_sha256": dossier_hash(execute)}}
        return operative

    def applied(self, operative):
        receipt = operative["review"]["approval_receipt"]
        return {"ok": True, "mode": "dossier_apply", "project": "demo", "approval_receipt": receipt,
                "review": {"required": True, "state": "completed", "approval_receipt": receipt},
                "execute_summary": {"ok": True, "mode": "execute", "results": [{"success": True,
                    "message_id": "source@example.test", "synthesis_targets": []}]},
                "verify_summary": {"ok": True, "mode": "verify", "results": [{"consistent": True,
                    "message_id": "source@example.test"}]},
                "synthesis_handoff": {"schema_version": 1, "status": "pending", "items": [{
                    "message_id": "source@example.test", "subject": "Synthetic workshop", "kind": "project",
                    "id": "demo", "synthesis_targets": [], "target_selection_required": True}]}}

    def project_handoff(self, event_id="2026-01-01-workshop", title="Synthetic workshop", start="2026-01-01"):
        operative = self.approved()
        applied = self.applied(operative)
        result = self.facade.process_request({
            "schema_version": 1, "stage": "handoff", "scope": self.scope(), "sources": [self.source()],
            "apply_request": operative, "apply_result": applied,
            "apply_result_sha256": canonical_json_sha256(applied),
            "event": {"owner_kind": "project", "owner_id": "demo", "id": event_id,
                      "title": title, "start": start}}, workspace_root=self.root)
        self.assertTrue(result["ok"], result)
        return result["work_order"]

    # --- topic fixture -------------------------------------------------------
    @staticmethod
    def topic_entry():
        return {"id": "topic-a", "title": "Topic A", "mailbox_folder": "Themen/Topic A", "subtopics": [{
            "id": "sub-a", "title": "Sub A", "events": [{
                "id": "event-a", "title": "Event A", "starts_on": "2026-02-01", "ends_on": "2026-02-03",
                "reference_md": "memory/references/topics/topic-a/subtopics/sub-a/events/event-a/index.md",
                "status": "active", "phase": "planned"}]}]}

    def rich_source(self, mid="source@example.test", sha="a" * 64):
        mid = mid.lower()
        return {"id": "mail-" + canonical_json_sha256(mid), "kind": "mail", "message_id": mid,
                "content_sha256": sha,
                "locations": [{"account": "synthetic", "folder": "INBOX", "envelope_id": "1", "message_id": mid}],
                "thread_parents": [], "calendar_objects": []}

    def rich_attachments(self, sha="a" * 64):
        return [{"id": "attachment-" + sha, "sha256": sha, "size_bytes": 20, "provenance": [
            {"account": "synthetic", "folder": "INBOX", "envelope_id": "1", "message_id": "source@example.test",
             "part_locator": "2", "filename": "program.pdf", "original_filename": "program.pdf",
             "mime_type": "application/pdf"}]}]

    def topic_work_order(self):
        source = self.rich_source()
        attachments = self.rich_attachments()
        order = {"schema_version": 1, "owner": "event-documentation", "status": "pending_content_review",
                 "event": {"owner_kind": "topic", "owner_id": "topic-a", "subtopic_id": "sub-a",
                           "id": "event-a", "title": "Event A", "start": "2026-02-01", "end": "2026-02-03"},
                 "sources": [source], "attachments": attachments,
                 "completeness": _completeness(self.scope())}
        order["source_snapshot_sha256"] = canonical_json_sha256({"sources": [source], "attachments": attachments})
        return order

    def content(self, ids):
        return {"overview": [{"text": "A scheduled workshop.", "source_ids": list(ids)}],
                "planned_agenda": [{"text": "Discuss options", "source_ids": list(ids)}],
                "observed_decisions": [],
                "reply_triage": [{"text": "No reply required", "source_ids": list(ids), "needed": False}],
                "todo_triage": [{"text": "Clarification pending", "source_ids": list(ids), "needed": False}]}

    def write(self, order):
        payload = {"schema_version": 1, "work_order": order, "content": self.content([s["id"] for s in order["sources"]])}
        return self.writer.apply_event(payload, workspace_root=self.root, lease_id=self.lease,
                                       trusted_workspace_lock_root=str(LOCK_ROOT))

    def report(self, order, result):
        return self.facade.process_request({"schema_version": 1, "stage": "report",
                                            "artifacts": result["artifacts"], "work_order": order,
                                            "output_receipt": result["output_receipt"]}, workspace_root=self.root)

    # --- tests ---------------------------------------------------------------
    def test_project_handoff_writer_report_round_trip(self):
        order = self.project_handoff()
        result = self.write(order)
        self.assertTrue(result["ok"], result)
        expected = [
            "memory/references/projects/demo/events/2026-01-01-workshop/index.md",
            "memory/evidence/projects/demo/events/2026-01-01-workshop/action-items.md",
            "memory/evidence/projects/demo/events/2026-01-01-workshop/source-log.json",
            "memory/evidence/projects/demo/events/2026-01-01-workshop/attachment-inventory.json",
            "memory/evidence/projects/demo/events/2026-01-01-workshop/compliance-report.json",
        ]
        self.assertEqual(expected, result["artifacts"])
        for rel in expected:
            self.assertEqual(hashlib.sha256((self.root / rel).read_bytes()).hexdigest(),
                             next(o["sha256"] for o in result["output_receipt"]["outputs"] if o["path"] == rel))
        report = self.report(order, result)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(expected), set(report["verified_outputs"]))
        self.assertIs(False, report["content_approved"])
        self.assertEqual("human_content_review_required", report["content_review"])
        # Report rejects any post-write content drift.
        (self.root / expected[2]).write_text("drift", encoding="utf-8")
        self.assertFalse(self.report(order, result)["ok"])

    def test_underscore_project_event_id_round_trips(self):
        order = self.project_handoff(event_id="workshop_2026")
        result = self.write(order)
        self.assertTrue(result["ok"], result)
        expected = [
            "memory/references/projects/demo/events/workshop_2026/index.md",
            "memory/evidence/projects/demo/events/workshop_2026/action-items.md",
            "memory/evidence/projects/demo/events/workshop_2026/source-log.json",
            "memory/evidence/projects/demo/events/workshop_2026/attachment-inventory.json",
            "memory/evidence/projects/demo/events/workshop_2026/compliance-report.json",
        ]
        self.assertEqual(expected, result["artifacts"])
        report = self.report(order, result)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(expected), set(report["verified_outputs"]))

    def test_topic_writer_and_extended_report(self):
        order = self.topic_work_order()
        result = self.write(order)
        self.assertTrue(result["ok"], result)
        expected = [
            "memory/references/topics/topic-a/subtopics/sub-a/events/event-a/index.md",
            "memory/evidence/topics/topic-a/events/event-a/action-items.md",
            "memory/evidence/topics/topic-a/events/event-a/source-log.json",
            "memory/evidence/topics/topic-a/events/event-a/attachment-inventory.json",
            "memory/evidence/topics/topic-a/events/event-a/compliance-report.json",
        ]
        self.assertEqual(expected, result["artifacts"])
        report = self.report(order, result)
        self.assertTrue(report["ok"], report)
        self.assertEqual(set(expected), set(report["verified_outputs"]))
        self.assertIs(False, report["content_approved"])

    def test_topic_report_rejects_path_outside_its_event_scope(self):
        order = self.topic_work_order()
        result = self.write(order)
        self.assertTrue(result["ok"], result)
        forged = copy.deepcopy(result)
        other = "memory/evidence/topics/topic-a/events/event-b/compliance-report.json"
        target = self.root / other
        target.parent.mkdir(parents=True)
        target.write_bytes((self.root / result["artifacts"][4]).read_bytes())
        forged["artifacts"] = result["artifacts"][:4] + [other]
        forged["output_receipt"]["outputs"] = [o for o in result["output_receipt"]["outputs"]
                                               if o["path"] != result["artifacts"][4]] + [
            {"path": other, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}]
        self.assertFalse(self.report(order, forged)["ok"])


if __name__ == "__main__":
    unittest.main()
