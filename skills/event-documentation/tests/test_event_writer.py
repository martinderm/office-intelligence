"""Synthetic source-bound event writes; no cloud or task synchronization."""
from __future__ import annotations
import copy
import hashlib
import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[3]
SCRIPT = BUNDLE / "skills" / "event-documentation" / "scripts" / "event_documentation.py"
LOCK = BUNDLE.parent / "workspace-lock" / "scripts" / "invoke_workspace_lock.py"

class EventWriterTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("event_documentation", SCRIPT)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        catalog = self.root / "memory/references/projects/projects.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"projects": [{"id": "demo", "title": "Demo", "status": "active",
            "mailbox_folder": "Projects/demo"}]}), encoding="utf-8")
        self.lease = "synthetic-event-writer"
        result = subprocess.run([sys.executable, "-B", str(LOCK), "acquire", str(self.root),
            "--harness", "synthetic-test", "--lease-id", self.lease, "--duration-minutes", "10", "--json"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)

    def payload(self):
        value = {"schema_version": 1, "event": {"owner_kind": "project", "owner_id": "demo",
                "id": "2026-01-01-workshop", "title": "Synthetic workshop", "start": "2026-01-01"},
                "sources": [{"id": "mail-1", "kind": "mail", "message_id": "source@example.test",
                             "content_sha256": "a" * 64}],
                "content": {"overview": [{"text": "A scheduled workshop.", "source_ids": ["mail-1"]}],
                            "planned_agenda": [{"text": "Discuss options", "source_ids": ["mail-1"]}],
                            "observed_decisions": [],
                            "reply_triage": [{"text": "No reply required", "source_ids": ["mail-1"], "needed": False}],
                            "todo_triage": [{"text": "Clarification pending", "source_ids": ["mail-1"], "needed": False}]},
                "completeness": {"exhaustive": False, "limitations": ["Only selected folders and first-page envelopes"]}}
        work_order = {"schema_version": 1, "owner": "event-documentation", "status": "pending_content_review",
            "event": value["event"], "sources": value["sources"], "attachments": [], "completeness": value["completeness"]}
        snapshot = {"sources": work_order["sources"], "attachments": work_order["attachments"]}
        digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        work_order["source_snapshot_sha256"] = digest
        return {"schema_version": 1, "work_order": work_order, "content": value["content"]}

    def apply(self, payload=None, lease=True):
        return self.api.apply_event(payload or self.payload(), workspace_root=self.root,
                                   lease_id=self.lease if lease else None)

    def test_dual_evidence_write_repeat_is_byte_identical_and_source_bound(self):
        first = self.apply()
        self.assertTrue(first["ok"])
        index = self.root / "memory/references/projects/demo/events/2026-01-01-workshop/index.md"
        evidence = self.root / "memory/evidence/projects/demo/events/2026-01-01-workshop"
        self.assertTrue(index.is_file())
        self.assertTrue((evidence / "action-items.md").is_file())
        self.assertTrue((evidence / "source-log.json").is_file())
        self.assertTrue((evidence / "attachment-inventory.json").is_file())
        self.assertTrue((evidence / "compliance-report.json").is_file())
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(self.apply()["ok"])
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        text = index.read_text(encoding="utf-8")
        self.assertIn("Discuss options", text)
        self.assertIn("mail-1", text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual([], self.payload()["content"]["observed_decisions"])

    def test_invalid_source_anchor_or_path_creates_no_event(self):
        for change in ("unknown-source", "path-escape", "unknown-key"):
            payload = self.payload()
            if change == "unknown-source":
                payload["content"]["planned_agenda"][0]["source_ids"] = ["missing"]
            elif change == "path-escape":
                payload["work_order"]["event"]["owner_id"] = "../escape"
            else:
                payload["cloud_sync"] = True
            with self.subTest(change=change):
                self.assertFalse(self.apply(payload)["ok"])
                self.assertFalse((self.root / "memory/evidence").exists())

    def test_conflicting_existing_target_preflight_preserves_all_existing_bytes(self):
        index = self.root / "memory/references/projects/demo/events/2026-01-01-workshop/index.md"
        index.parent.mkdir(parents=True)
        index.write_text("Existing human notes", encoding="utf-8")
        self.assertFalse(self.apply()["ok"])
        self.assertEqual("Existing human notes", index.read_text(encoding="utf-8"))
        self.assertFalse((self.root / "memory/evidence").exists())

    def test_foreign_missing_lock_and_no_approval_claim(self):
        self.assertFalse(self.apply(lease=False)["ok"])
        self.assertFalse((self.root / "memory/evidence").exists())
        result = self.apply()
        self.assertTrue(result["ok"])
        self.assertEqual("human_content_review_required", result["content_review"])
        self.assertNotIn("approval_receipt", result)

    def test_reply_and_todo_require_independent_boolean_decisions(self):
        payload = self.payload()
        payload["content"]["reply_triage"][0]["needed"] = True
        payload["content"]["todo_triage"][0]["needed"] = False
        self.assertTrue(self.apply(payload)["ok"])
        payload = self.payload()
        payload["content"]["todo_triage"][0]["needed"] = "yes"
        self.assertFalse(self.apply(payload)["ok"])

    def test_source_snapshot_drift_and_uncatalogued_owner_fail_before_writes(self):
        for kind in ("source-drift", "unknown-owner"):
            payload = self.payload()
            if kind == "source-drift":
                payload["work_order"]["sources"][0]["content_sha256"] = "b" * 64
            else:
                payload["work_order"]["event"]["owner_id"] = "missing"
            with self.subTest(kind=kind):
                self.assertFalse(self.apply(payload)["ok"])
                self.assertFalse((self.root / "memory/evidence").exists())

    def test_reparse_parent_is_rejected_before_event_mutation(self):
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / "memory/evidence"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("Platform does not permit synthetic directory symlinks")
            self.assertFalse(self.apply()["ok"])
            self.assertEqual([], list(Path(outside).iterdir()))
            self.assertFalse((self.root / "memory/references/projects/demo/events").exists())
