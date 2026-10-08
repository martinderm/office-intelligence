"""Synthetic MIME-to-quarantine regressions; no live mailbox transport."""
from __future__ import annotations
import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import attachments, himalaya
from core.quarantine import attachment_fetch as fetch

class AttachmentBasenameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("verify_workspace_lock", "verify_no_tracked_quarantine"):
            mock = patch.object(fetch, name, return_value=None)
            mock.start()
            self.addCleanup(mock.stop)
        blocker = patch.object(himalaya, "run_himalaya", side_effect=AssertionError("No real mailbox"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def prepared(self, filename="agenda..pdf", content=b"%PDF-1.4 synthetic program"):
        eml = attachments.build_test_eml(subject="Synthetic event", message_id="<source@example.test>",
            attachments=[{"filename": filename, "mime_type": "application/pdf", "data": content}])
        candidate = attachments.bind_attachment_candidate(attachments.inspect_mime_tree(eml)[0],
            account="synthetic", folder="INBOX", envelope_id="1", message_id="source@example.test")
        digest = hashlib.sha256(content).hexdigest()
        review = fetch.compute_review_hash(account="synthetic", folder="INBOX", envelope_id="1",
            message_id="source@example.test", part_locator=candidate["part_locator"], inventory_sha256=digest)
        return dict(candidate=candidate, account="synthetic", folder="INBOX", envelope_id="1",
            message_id="source@example.test", part_locator=candidate["part_locator"], inventory_sha256=digest,
            review_hash=review, approval_receipt={"receipt_id": "synthetic-human-review",
                "request_hash": review, "approved_at": "2026-01-01T12:00:00Z", "approved_by": "human-fixture"},
            raw_eml=eml, run_id="synthetic_event", data_dir=self.root / "data" / "mail-desk")

    def test_embedded_dots_fetch_and_repeat_preserve_original_mime_name(self):
        request = self.prepared()
        self.assertEqual("agenda..pdf", request["candidate"]["original_filename"])
        first = fetch.op_attachment_fetch(**request)
        self.assertEqual("fetched", first["status"])
        self.assertEqual("agenda..pdf", first["original_filename"])
        target = self.root / first["relative_path"]
        self.assertEqual(request["inventory_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
        second = fetch.op_attachment_fetch(**request)
        self.assertEqual("already_fetched", second["status"])
        self.assertEqual("agenda..pdf", second["original_filename"])
        self.assertEqual(first["relative_path"], second["relative_path"])

    def test_raw_mime_traversal_is_not_laundered_by_inventory_sanitization(self):
        for name in ("../agenda.pdf", "folder/agenda.pdf", "folder\\agenda.pdf", "C:agenda.pdf"):
            with self.subTest(name=name):
                request = self.prepared(name)
                self.assertEqual(name, request["candidate"]["original_filename"])
                with self.assertRaises(ValueError):
                    fetch.op_attachment_fetch(**request)
                self.assertFalse((self.root / "data").exists())

    def test_unsafe_basename_aliases_are_rejected_before_writes(self):
        for name in ("..", ".", "CON.pdf", "NUL.pdf", "agenda.pdf.", "agenda.pdf ", "agenda\t.pdf"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    fetch.validate_attachment_filename(name)

    def test_identity_hash_and_original_name_drift_remain_fail_closed(self):
        base = self.prepared("agenda.pdf")
        for field, value in (("account", "other"), ("message_id", "other@example.test"),
                             ("sha256", "0" * 64), ("original_filename", "../agenda.pdf")):
            request = copy.deepcopy(base)
            request["candidate"][field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    fetch.op_attachment_fetch(**request)
                self.assertFalse((self.root / "data").exists())

    def test_raw_payload_drift_denied(self):
        request = self.prepared("agenda.pdf")
        request["raw_eml"] = self.prepared("agenda.pdf", b"%PDF-1.4 changed bytes")["raw_eml"]
        with self.assertRaises(ValueError):
            fetch.op_attachment_fetch(**request)
        self.assertFalse(any((self.root / "data").rglob("*.pdf")))

    def test_embedded_double_dot_basename_is_not_a_traversal_component(self):
        self.assertEqual("agenda..pdf", fetch.validate_attachment_filename("agenda..pdf"))
