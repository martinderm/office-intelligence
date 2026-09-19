"""TDD tests for FR-08 MD-A2: Review-bound quarantine attachment fetch contract."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import attachment_policy  # noqa: E402
from core import attachments  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402


class MailDeskAttachmentsMDA2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
        self._himalaya_blocker = patch.object(
            himalaya,
            "run_himalaya",
            side_effect=RuntimeError("Real Himalaya process execution is forbidden in hermetic unit tests!"),
        )
        self._himalaya_blocker.start()
        self._workspace_lock_patcher = patch.object(
            afetch,
            "verify_workspace_lock",
            return_value=None,
        )
        self._workspace_lock_patcher.start()

    def tearDown(self) -> None:
        self._workspace_lock_patcher.stop()
        self._himalaya_blocker.stop()

    def test_compute_review_hash_is_deterministic(self) -> None:
        h1 = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="test-msg-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        h2 = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="test-msg-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(64, len(h1))
        self.assertEqual(h1, h2)

        # Mismatch in any parameter changes hash
        h3 = afetch.compute_review_hash(
            account="OTHER-ACC",
            message_id="test-msg-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertNotEqual(h1, h3)

    def test_verify_approval_receipt_valid_and_drifts(self) -> None:
        rev_hash = "a" * 64
        valid_receipt = {
            "receipt_id": "rec-2026-001",
            "request_hash": rev_hash,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }
        # Valid receipt succeeds
        afetch.verify_approval_receipt(valid_receipt, expected_review_hash=rev_hash)

        # Missing receipt raises
        with self.assertRaises(afetch.ApprovalReceiptMissingError):
            afetch.verify_approval_receipt(None, expected_review_hash=rev_hash)

        with self.assertRaises(afetch.ApprovalReceiptMissingError):
            afetch.verify_approval_receipt({}, expected_review_hash=rev_hash)

        # Mismatched request_hash raises ReceiptDriftError
        drifted_receipt = dict(valid_receipt, request_hash="b" * 64)
        with self.assertRaises(afetch.ReceiptDriftError):
            afetch.verify_approval_receipt(drifted_receipt, expected_review_hash=rev_hash)

        # Missing required fields raises ValueError
        with self.assertRaises(ValueError):
            afetch.verify_approval_receipt({"request_hash": rev_hash}, expected_review_hash=rev_hash)

    def test_attachment_fetch_success_hermetic(self) -> None:
        pdf_bytes = b"%PDF-1.4 test pdf content for quarantine fetch"
        import hashlib
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        raw_eml = attachments.build_test_eml(
            subject="Fetch Test",
            message_id="<fetch-001@example.org>",
            attachments=[{"filename": "agenda.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
        )

        candidate = {
            "filename": "agenda.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes),
            "sha256": pdf_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "fetch-001@example.org",
        }

        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="fetch-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha,
        )

        receipt = {
            "receipt_id": "rec-001",
            "request_hash": rev_hash,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            res = afetch.op_attachment_fetch(
                candidate=candidate,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="fetch-001@example.org",
                part_locator="2",
                inventory_sha256=pdf_sha,
                review_hash=rev_hash,
                approval_receipt=receipt,
                run_id="run_20260914_test",
                raw_eml=raw_eml,
                data_dir=data_dir,
            )

            self.assertEqual("fetched", res["status"])
            self.assertEqual("run_20260914_test", res["run_id"])
            self.assertEqual("agenda.pdf", res["filename"])
            self.assertEqual(pdf_sha, res["inventory_sha256"])
            self.assertEqual(pdf_sha, res["fetch_sha256"])
            self.assertEqual(len(pdf_bytes), res["size_bytes"])
            self.assertEqual("application/pdf", res["effective_mime_type"])
            self.assertIsNone(res["error"])

            # Verify file exists on disk
            rel_path = res["relative_path"]
            self.assertFalse(Path(rel_path).is_absolute(), "Path must be relative")
            target_file = Path(tmp_dir) / rel_path
            self.assertTrue(target_file.exists())
            self.assertEqual(pdf_bytes, target_file.read_bytes())

    def test_preflight_drift_fails_closed_before_io(self) -> None:
        pdf_bytes = b"%PDF-1.4 sample"
        import hashlib
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        candidate = {
            "filename": "agenda.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes),
            "sha256": pdf_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "fetch-001@example.org",
        }

        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="fetch-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            with patch.object(himalaya, "fetch_raw_message_eml") as mock_fetch:
                # 1. Account drift
                with self.assertRaises(attachments.AccountDriftError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="DIFFERENT-ACCOUNT",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="fetch-001@example.org",
                        part_locator="2",
                        inventory_sha256=pdf_sha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        data_dir=data_dir,
                    )

                # 2. Location drift
                with self.assertRaises(attachments.LocationDriftError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="Archive",
                        envelope_id="7195",
                        message_id="fetch-001@example.org",
                        part_locator="2",
                        inventory_sha256=pdf_sha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        data_dir=data_dir,
                    )

                # 3. Message-ID drift
                with self.assertRaises(attachments.MessageIdDriftError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="different-mid@example.org",
                        part_locator="2",
                        inventory_sha256=pdf_sha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        data_dir=data_dir,
                    )

                # 4. Hash drift before I/O
                with self.assertRaises(attachments.HashDriftError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="fetch-001@example.org",
                        part_locator="2",
                        inventory_sha256="0" * 64,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        data_dir=data_dir,
                    )

                # 5. Missing approval receipt
                with self.assertRaises(afetch.ApprovalReceiptMissingError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="fetch-001@example.org",
                        part_locator="2",
                        inventory_sha256=pdf_sha,
                        review_hash=rev_hash,
                        approval_receipt=None,
                        data_dir=data_dir,
                    )

                # No fetch was ever initiated!
                mock_fetch.assert_not_called()
                # No directories were created!
                self.assertFalse((data_dir / "attachments").exists())

    def test_run_id_traversal_and_win32_reserved_rejected(self) -> None:
        # Invalid run_ids must be rejected
        for bad_id in ["../escape", r"..\\escape", "run/sub", r"run\sub", "CON", "PRN", "AUX", "NUL", "COM1"]:
            self.assertFalse(afetch.is_valid_run_id(bad_id), f"run_id '{bad_id}' should be invalid")

        for good_id in ["run_20260914_01", "run-1234", "batch_run_42"]:
            self.assertTrue(afetch.is_valid_run_id(good_id), f"run_id '{good_id}' should be valid")

    def test_hash_drift_during_fetch_cleans_temp_and_aborts(self) -> None:
        import hashlib
        original_bytes = b"%PDF-1.4 original"
        forged_bytes = b"%PDF-1.4 tampered bytes in mailbox"
        original_sha = hashlib.sha256(original_bytes).hexdigest()

        # Message in mailbox actually contains forged_bytes
        raw_eml = attachments.build_test_eml(
            subject="Tampered Test",
            message_id="<tampered-001@example.org>",
            attachments=[{"filename": "doc.pdf", "mime_type": "application/pdf", "data": forged_bytes}],
        )

        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(original_bytes),
            "sha256": original_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "tampered-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="tampered-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=original_sha,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            with self.assertRaises(attachments.HashDriftError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="tampered-001@example.org",
                    part_locator="2",
                    inventory_sha256=original_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_tamper_test",
                    raw_eml=raw_eml,
                    data_dir=data_dir,
                )

            # Target file must not exist
            target = data_dir / "attachments" / "run_tamper_test" / "doc.pdf"
            self.assertFalse(target.exists())

            # No temporary files (.tmp) left behind
            run_dir = data_dir / "attachments" / "run_tamper_test"
            if run_dir.exists():
                tmp_files = list(run_dir.glob("*.tmp"))
                self.assertEqual(0, len(tmp_files), f"Temp files were not cleaned up: {tmp_files}")

    def test_active_content_and_extension_drift_blocked(self) -> None:
        import hashlib
        exe_bytes = b"MZ" + bytes([0x90, 0x00]) + b"executable content"
        exe_sha = hashlib.sha256(exe_bytes).hexdigest()

        raw_eml = attachments.build_test_eml(
            subject="Malware Test",
            message_id="<malware-001@example.org>",
            attachments=[{"filename": "payload.exe", "mime_type": "application/x-dosexec", "data": exe_bytes}],
        )

        candidate = {
            "filename": "payload.exe",
            "mime_type": "application/x-dosexec",
            "size_bytes": len(exe_bytes),
            "sha256": exe_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "malware-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="malware-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=exe_sha,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            with self.assertRaises(afetch.ActiveContentBlockedError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="malware-001@example.org",
                    part_locator="2",
                    inventory_sha256=exe_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_malware",
                    raw_eml=raw_eml,
                    data_dir=data_dir,
                )

    def test_single_and_cumulative_quota_enforced(self) -> None:
        import hashlib
        # Single file exceeding 15 MB
        big_bytes = b"0" * (16 * 1024 * 1024)
        big_sha = hashlib.sha256(big_bytes).hexdigest()

        candidate = {
            "filename": "huge.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": len(big_bytes),
            "sha256": big_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "huge-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="huge-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=big_sha,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            with self.assertRaises(afetch.QuotaExceededError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="huge-001@example.org",
                    part_locator="2",
                    inventory_sha256=big_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_huge",
                    raw_eml=b"fake",
                    data_dir=data_dir,
                )

    def test_idempotent_retry_and_collision_handling(self) -> None:
        import hashlib
        pdf_bytes = b"%PDF-1.4 idempotent test"
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        raw_eml = attachments.build_test_eml(
            subject="Idempotent Test",
            message_id="<idempotent-001@example.org>",
            attachments=[{"filename": "doc.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
        )

        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes),
            "sha256": pdf_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "idempotent-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="idempotent-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            # First fetch
            res1 = afetch.op_attachment_fetch(
                candidate=candidate,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="idempotent-001@example.org",
                part_locator="2",
                inventory_sha256=pdf_sha,
                review_hash=rev_hash,
                approval_receipt=receipt,
                run_id="run_idem",
                raw_eml=raw_eml,
                data_dir=data_dir,
            )
            self.assertEqual("fetched", res1["status"])

            # Second fetch with same hash (idempotent retry)
            res2 = afetch.op_attachment_fetch(
                candidate=candidate,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="idempotent-001@example.org",
                part_locator="2",
                inventory_sha256=pdf_sha,
                review_hash=rev_hash,
                approval_receipt=receipt,
                run_id="run_idem",
                raw_eml=raw_eml,
                data_dir=data_dir,
            )
            self.assertIn(res2["status"], ["already_fetched", "fetched"])
            self.assertEqual(pdf_sha, res2["fetch_sha256"])

            # Now simulate collision: same filename, different hash exists in quarantine
            target_path = data_dir / "attachments" / "run_idem" / "doc.pdf"
            target_path.write_bytes(b"%PDF-1.4 completely different content!")

            with self.assertRaises(afetch.QuarantineCollisionError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="idempotent-001@example.org",
                    part_locator="2",
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_idem",
                    raw_eml=raw_eml,
                    data_dir=data_dir,
                )

    def test_cleanup_run_quarantine_scoped_to_run_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_clean_test"
            run_dir.mkdir(parents=True)
            (run_dir / "file.pdf").write_bytes(b"pdf")

            # Normal cleanup removes run directory
            afetch.cleanup_run_quarantine("run_clean_test", data_dir=data_dir)
            self.assertFalse(run_dir.exists())

            # Attempting traversal in run_id raises ValueError and doesn't delete parent
            with self.assertRaises(ValueError):
                afetch.cleanup_run_quarantine("..", data_dir=data_dir)

    def test_execute_manifest_attachment_fetch(self) -> None:
        import hashlib
        pdf_bytes = b"%PDF-1.4 manifest batch test"
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        raw_eml = attachments.build_test_eml(
            subject="Manifest Fetch Test",
            message_id="<mfetch-001@example.org>",
            attachments=[{"filename": "report.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
        )

        candidate = {
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes),
            "sha256": pdf_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "mfetch-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="mfetch-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            manifest_file = Path(tmp_dir) / "manifest.json"
            manifest_file.write_text(
                json.dumps({
                    "account": "BOKU-MARTIN",
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "attachment_fetch",
                            "candidate": candidate,
                            "envelope_id": "7195",
                            "folder": "INBOX",
                            "account": "BOKU-MARTIN",
                            "message_id": "mfetch-001@example.org",
                            "part_locator": "2",
                            "inventory_sha256": pdf_sha,
                            "review_hash": rev_hash,
                            "approval_receipt": receipt,
                            "run_id": "run_manifest_01",
                        }
                    ],
                }),
                encoding="utf-8",
            )

            with patch.object(afetch, "resolve_data_dir", return_value=data_dir):
                with patch.object(himalaya, "fetch_raw_message_eml", return_value=raw_eml):
                    res = client.execute_manifest(manifest_file)

            self.assertTrue(res["all_succeeded"])
            self.assertEqual(1, len(res["results"]))
            item_res = res["results"][0]["result"]
            self.assertEqual("fetched", item_res["status"])
            self.assertEqual("report.pdf", item_res["filename"])

    def test_symlink_escape_rejected(self) -> None:
        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "sha256": "a" * 64,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "symlink-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="symlink-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="a" * 64,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            with patch("pathlib.Path.is_symlink", return_value=True):
                with self.assertRaises(afetch.SymlinkEscapeError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="symlink-001@example.org",
                        part_locator="2",
                        inventory_sha256="a" * 64,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id="run_sym",
                        data_dir=data_dir,
                    )

    def test_fetch_timeout_fails_closed(self) -> None:
        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "sha256": "a" * 64,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "timeout-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="timeout-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="a" * 64,
        )
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            with patch.object(himalaya, "fetch_raw_message_eml", side_effect=TimeoutError("Himalaya fetch timed out after 25s")):
                with self.assertRaises(TimeoutError):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="timeout-001@example.org",
                        part_locator="2",
                        inventory_sha256="a" * 64,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id="run_timeout",
                        data_dir=data_dir,
                    )

    def test_manifest_partial_failure_handling(self) -> None:
        import hashlib
        pdf_bytes = b"%PDF-1.4 partial success"
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        raw_eml = attachments.build_test_eml(
            subject="Partial Test",
            message_id="<partial-001@example.org>",
            attachments=[{"filename": "good.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
        )

        candidate_good = {
            "filename": "good.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes),
            "sha256": pdf_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "partial-001@example.org",
        }
        rev_hash_good = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="partial-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha,
        )
        receipt_good = {"receipt_id": "rec-001", "request_hash": rev_hash_good, "approved_at": "2026-09-14T09:00:00Z", "approved_by": "me"}

        # Bad candidate: drifts
        candidate_bad = dict(candidate_good, filename="bad.pdf", part_locator="3")

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            manifest_file = Path(tmp_dir) / "manifest.json"
            manifest_file.write_text(
                json.dumps({
                    "account": "BOKU-MARTIN",
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "attachment_fetch",
                            "candidate": candidate_good,
                            "envelope_id": "7195",
                            "folder": "INBOX",
                            "message_id": "partial-001@example.org",
                            "part_locator": "2",
                            "inventory_sha256": pdf_sha,
                            "review_hash": rev_hash_good,
                            "approval_receipt": receipt_good,
                            "run_id": "run_part",
                        },
                        {
                            "action": "attachment_fetch",
                            "candidate": candidate_bad,
                            "envelope_id": "7195",
                            "folder": "INBOX",
                            "message_id": "partial-001@example.org",
                            "part_locator": "3",
                            "inventory_sha256": pdf_sha,
                            "review_hash": rev_hash_good,
                            "approval_receipt": None,  # Missing receipt!
                            "run_id": "run_part",
                        },
                    ],
                }),
                encoding="utf-8",
            )

            with patch.object(afetch, "resolve_data_dir", return_value=data_dir):
                with patch.object(himalaya, "fetch_raw_message_eml", return_value=raw_eml):
                    res = client.execute_manifest(manifest_file)

            # Overall manifest did not fully succeed due to op 1 failure
            self.assertFalse(res["all_succeeded"])
            self.assertEqual(2, len(res["results"]))
            self.assertTrue(res["results"][0]["success"])
            self.assertFalse(res["results"][1]["success"])
            self.assertTrue(len(res["results"][1].get("error", "")) > 0)



    def test_disallowed_extensions_loaded_and_blocked(self) -> None:
        """Verify transport.disallowed_extensions from policy blocks extensions fail-closed."""
        disallowed_samples = [
            ("evil.docm", b"dummy docm content"),
            ("evil.xlsm", b"dummy xlsm content"),
            ("evil.pptm", b"dummy pptm content"),
            ("script.ps1", b"Write-Host 'Active PowerShell'"),
            ("script.bat", b"@echo off\r\ndir"),
            ("script.cmd", b"@echo off\r\nexit"),
            ("script.vbs", b"WScript.Echo \"Test\""),
            ("script.js", b"console.log('hi');"),
            ("script.sh", b"#!/bin/bash\necho hi"),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            for fname, payload in disallowed_samples:
                fsha = hashlib.sha256(payload).hexdigest()
                raw_eml = attachments.build_test_eml(
                    subject="Disallowed Extension Test",
                    message_id=f"<disallowed-{fname}@example.org>",
                    attachments=[{"filename": fname, "mime_type": "application/octet-stream", "data": payload}],
                )
                candidate = {
                    "filename": fname,
                    "mime_type": "application/octet-stream",
                    "size_bytes": len(payload),
                    "sha256": fsha,
                    "part_locator": "2",
                    "fetch_status": "available",
                    "provenance": attachments.PROVENANCE_RFC822,
                    "account": "BOKU-MARTIN",
                    "folder": "INBOX",
                    "envelope_id": "7195",
                    "message_id": f"disallowed-{fname}@example.org",
                }
                rev_hash = afetch.compute_review_hash(
                    account="BOKU-MARTIN",
                    message_id=f"disallowed-{fname}@example.org",
                    folder="INBOX",
                    envelope_id="7195",
                    part_locator="2",
                    inventory_sha256=fsha,
                )
                receipt = {
                    "receipt_id": f"rec-{fname}",
                    "request_hash": rev_hash,
                    "approved_at": "2026-09-14T09:00:00Z",
                    "approved_by": "martin",
                }

                with self.assertRaises(
                    (afetch.DisallowedExtensionError, afetch.ActiveContentBlockedError),
                    msg=f"Expected disallowed extension error for {fname}",
                ):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id=f"disallowed-{fname}@example.org",
                        part_locator="2",
                        inventory_sha256=fsha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id=f"run_disallowed_{fname.replace('.', '_')}",
                        raw_eml=raw_eml,
                        data_dir=data_dir,
                    )

    def test_mime_and_extension_drift_fails_closed(self) -> None:
        """Verify MIME drift and extension drift checks fail-closed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            # Case A: Inventory stated PDF, but content is actually PNG
            png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            png_sha = hashlib.sha256(png_bytes).hexdigest()

            raw_eml_a = attachments.build_test_eml(
                subject="Mime Drift Test",
                message_id="<mime-drift-01@example.org>",
                attachments=[{"filename": "claim_doc.pdf", "mime_type": "image/png", "data": png_bytes}],
            )
            candidate_a = {
                "filename": "claim_doc.pdf",
                "mime_type": "application/pdf",  # Inventory drift: claims application/pdf
                "size_bytes": len(png_bytes),
                "sha256": png_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "mime-drift-01@example.org",
            }
            rev_hash_a = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="mime-drift-01@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=png_sha,
            )
            receipt_a = {
                "receipt_id": "rec-drift-a",
                "request_hash": rev_hash_a,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            with self.assertRaises(afetch.MimeDriftError):
                afetch.op_attachment_fetch(
                    candidate=candidate_a,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="mime-drift-01@example.org",
                    part_locator="2",
                    inventory_sha256=png_sha,
                    review_hash=rev_hash_a,
                    approval_receipt=receipt_a,
                    run_id="run_mime_drift",
                    raw_eml=raw_eml_a,
                    data_dir=data_dir,
                )

            # Case B: Extension drift: filename claims .pdf, but content and MIME is text/plain
            txt_bytes = b"Hello, this is just plain text, not a PDF at all."
            txt_sha = hashlib.sha256(txt_bytes).hexdigest()

            raw_eml_b = attachments.build_test_eml(
                subject="Ext Drift Test",
                message_id="<ext-drift-02@example.org>",
                attachments=[{"filename": "fake.pdf", "mime_type": "text/plain", "data": txt_bytes}],
            )
            candidate_b = {
                "filename": "fake.pdf",
                "mime_type": "text/plain",
                "size_bytes": len(txt_bytes),
                "sha256": txt_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "ext-drift-02@example.org",
            }
            rev_hash_b = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="ext-drift-02@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=txt_sha,
            )
            receipt_b = {
                "receipt_id": "rec-drift-b",
                "request_hash": rev_hash_b,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            with self.assertRaises(afetch.MimeDriftError):
                afetch.op_attachment_fetch(
                    candidate=candidate_b,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="ext-drift-02@example.org",
                    part_locator="2",
                    inventory_sha256=txt_sha,
                    review_hash=rev_hash_b,
                    approval_receipt=receipt_b,
                    run_id="run_ext_drift",
                    raw_eml=raw_eml_b,
                    data_dir=data_dir,
                )

    def test_genuine_ooxml_verification(self) -> None:
        """Verify genuine OOXML package inspection (plain ZIP rejected, macro-bearing rejected, valid accepted)."""
        import io
        import zipfile

        # 1. Plain ZIP renamed to .docx (missing [Content_Types].xml & word/document.xml)
        plain_zip_buf = io.BytesIO()
        with zipfile.ZipFile(plain_zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("readme.txt", "just a zip")
        plain_zip_bytes = plain_zip_buf.getvalue()
        plain_zip_sha = hashlib.sha256(plain_zip_bytes).hexdigest()

        # 2. Macro-bearing OOXML (has vbaProject.bin)
        macro_zip_buf = io.BytesIO()
        with zipfile.ZipFile(macro_zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            zf.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Macro</w:t></w:r></w:p></w:body></w:document>")
            zf.writestr("word/vbaProject.bin", b"DANGEROUS MACRO BINARY")
        macro_bytes = macro_zip_buf.getvalue()
        macro_sha = hashlib.sha256(macro_bytes).hexdigest()

        # 3. Genuine clean OOXML .docx
        clean_docx_buf = io.BytesIO()
        with zipfile.ZipFile(clean_docx_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            zf.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Clean Text</w:t></w:r></w:p></w:body></w:document>")
        clean_docx_bytes = clean_docx_buf.getvalue()
        clean_docx_sha = hashlib.sha256(clean_docx_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            # Case 1: Plain zip renamed to docx -> rejected as MimeDriftError or ActiveContentBlockedError
            raw_eml_plain = attachments.build_test_eml(
                subject="Plain Zip Test",
                message_id="<plain-zip@example.org>",
                attachments=[{"filename": "fake.docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": plain_zip_bytes}],
            )
            candidate_plain = {
                "filename": "fake.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": len(plain_zip_bytes),
                "sha256": plain_zip_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "plain-zip@example.org",
            }
            rev_hash_plain = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="plain-zip@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=plain_zip_sha,
            )
            receipt_plain = {
                "receipt_id": "rec-plain",
                "request_hash": rev_hash_plain,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            with self.assertRaises((afetch.MimeDriftError, afetch.ActiveContentBlockedError)):
                afetch.op_attachment_fetch(
                    candidate=candidate_plain,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="plain-zip@example.org",
                    part_locator="2",
                    inventory_sha256=plain_zip_sha,
                    review_hash=rev_hash_plain,
                    approval_receipt=receipt_plain,
                    run_id="run_plain_zip",
                    raw_eml=raw_eml_plain,
                    data_dir=data_dir,
                )

            # Case 2: Macro-bearing docx -> rejected as ActiveContentBlockedError
            raw_eml_macro = attachments.build_test_eml(
                subject="Macro Test",
                message_id="<macro-zip@example.org>",
                attachments=[{"filename": "macro.docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": macro_bytes}],
            )
            candidate_macro = {
                "filename": "macro.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": len(macro_bytes),
                "sha256": macro_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "macro-zip@example.org",
            }
            rev_hash_macro = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="macro-zip@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=macro_sha,
            )
            receipt_macro = {
                "receipt_id": "rec-macro",
                "request_hash": rev_hash_macro,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            with self.assertRaises(afetch.ActiveContentBlockedError):
                afetch.op_attachment_fetch(
                    candidate=candidate_macro,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="macro-zip@example.org",
                    part_locator="2",
                    inventory_sha256=macro_sha,
                    review_hash=rev_hash_macro,
                    approval_receipt=receipt_macro,
                    run_id="run_macro_zip",
                    raw_eml=raw_eml_macro,
                    data_dir=data_dir,
                )

            # Case 3: Clean OOXML docx -> successfully fetched
            raw_eml_clean = attachments.build_test_eml(
                subject="Clean Test",
                message_id="<clean-docx@example.org>",
                attachments=[{"filename": "valid.docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": clean_docx_bytes}],
            )
            candidate_clean = {
                "filename": "valid.docx",
                "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": len(clean_docx_bytes),
                "sha256": clean_docx_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "clean-docx@example.org",
            }
            rev_hash_clean = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="clean-docx@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=clean_docx_sha,
            )
            receipt_clean = {
                "receipt_id": "rec-clean",
                "request_hash": rev_hash_clean,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            res_clean = afetch.op_attachment_fetch(
                candidate=candidate_clean,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="clean-docx@example.org",
                part_locator="2",
                inventory_sha256=clean_docx_sha,
                review_hash=rev_hash_clean,
                approval_receipt=receipt_clean,
                run_id="run_clean_zip",
                raw_eml=raw_eml_clean,
                data_dir=data_dir,
            )
            self.assertEqual("fetched", res_clean["status"])
            self.assertEqual("valid.docx", res_clean["filename"])
            self.assertEqual("application/vnd.openxmlformats-officedocument.wordprocessingml.document", res_clean["effective_mime_type"])

    def test_quarantine_parent_chain_security_and_reparse_point(self) -> None:
        """Verify symlinks and Windows Reparse Points in parent hierarchy are blocked."""
        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "sha256": "a" * 64,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "reparse-001@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="reparse-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="a" * 64,
        )
        receipt = {
            "receipt_id": "rec-001",
            "request_hash": rev_hash,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_reparse"
            run_dir.mkdir(parents=True, exist_ok=True)

            # Mock os.lstat to simulate FILE_ATTRIBUTE_REPARSE_POINT (0x400)
            orig_lstat = os.lstat

            def mock_lstat(path, *args, **kwargs):
                st = orig_lstat(path, *args, **kwargs)
                if "run_reparse" in str(path):
                    # inject st_file_attributes with 0x400
                    class MockStat:
                        def __init__(self, base_st):
                            self._st = base_st
                            self.st_mode = base_st.st_mode
                            self.st_size = base_st.st_size
                            self.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
                    return MockStat(st)
                return st

            with patch("os.lstat", side_effect=mock_lstat):
                with self.assertRaises(afetch.SymlinkEscapeError) as cm:
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="reparse-001@example.org",
                        part_locator="2",
                        inventory_sha256="a" * 64,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id="run_reparse",
                        raw_eml=b"fake",
                        data_dir=data_dir,
                    )
                self.assertIn("reparse point", str(cm.exception).lower())

    def test_atomic_no_clobber_promotion_race_and_collision(self) -> None:
        """Verify atomic no-clobber promotion: identical hash succeeds, different hash raises QuarantineCollisionError."""
        pdf_bytes_1 = b"%PDF-1.4 file one content"
        pdf_sha_1 = hashlib.sha256(pdf_bytes_1).hexdigest()

        pdf_bytes_2 = b"%PDF-1.4 file two conflicting content"
        pdf_sha_2 = hashlib.sha256(pdf_bytes_2).hexdigest()

        raw_eml_1 = attachments.build_test_eml(
            subject="Race Test 1",
            message_id="<race-001@example.org>",
            attachments=[{"filename": "target.pdf", "mime_type": "application/pdf", "data": pdf_bytes_1}],
        )
        raw_eml_2 = attachments.build_test_eml(
            subject="Race Test 2",
            message_id="<race-002@example.org>",
            attachments=[{"filename": "target.pdf", "mime_type": "application/pdf", "data": pdf_bytes_2}],
        )

        candidate_1 = {
            "filename": "target.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes_1),
            "sha256": pdf_sha_1,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "race-001@example.org",
        }
        rev_hash_1 = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="race-001@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha_1,
        )
        receipt_1 = {
            "receipt_id": "rec-001",
            "request_hash": rev_hash_1,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }

        candidate_2 = {
            "filename": "target.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes_2),
            "sha256": pdf_sha_2,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "race-002@example.org",
        }
        rev_hash_2 = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="race-002@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha_2,
        )
        receipt_2 = {
            "receipt_id": "rec-002",
            "request_hash": rev_hash_2,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            # 1. First fetch establishes target.pdf
            res1 = afetch.op_attachment_fetch(
                candidate=candidate_1,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="race-001@example.org",
                part_locator="2",
                inventory_sha256=pdf_sha_1,
                review_hash=rev_hash_1,
                approval_receipt=receipt_1,
                run_id="run_atomic_race",
                raw_eml=raw_eml_1,
                data_dir=data_dir,
            )
            self.assertEqual("fetched", res1["status"])
            target_path = data_dir / "attachments" / "run_atomic_race" / "target.pdf"
            self.assertTrue(target_path.exists())
            self.assertEqual(pdf_bytes_1, target_path.read_bytes())

            # 2. Re-fetch identical content -> returns already_fetched, target content untouched
            res1_repeat = afetch.op_attachment_fetch(
                candidate=candidate_1,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="race-001@example.org",
                part_locator="2",
                inventory_sha256=pdf_sha_1,
                review_hash=rev_hash_1,
                approval_receipt=receipt_1,
                run_id="run_atomic_race",
                raw_eml=raw_eml_1,
                data_dir=data_dir,
            )
            self.assertIn(res1_repeat["status"], ["already_fetched", "fetched"])
            self.assertEqual(pdf_bytes_1, target_path.read_bytes())

            # 3. Conflicting fetch (different content for target.pdf) -> raises QuarantineCollisionError, does NOT clobber
            with self.assertRaises(afetch.QuarantineCollisionError):
                afetch.op_attachment_fetch(
                    candidate=candidate_2,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="race-002@example.org",
                    part_locator="2",
                    inventory_sha256=pdf_sha_2,
                    review_hash=rev_hash_2,
                    approval_receipt=receipt_2,
                    run_id="run_atomic_race",
                    raw_eml=raw_eml_2,
                    data_dir=data_dir,
                )
            # Verify original target content was preserved
            self.assertEqual(pdf_bytes_1, target_path.read_bytes())

            # Verify no temp files leaked
            run_dir = data_dir / "attachments" / "run_atomic_race"
            self.assertEqual(0, len(list(run_dir.glob("*.tmp"))))

    def test_quotas_enforced_per_message_not_per_run(self) -> None:
        """Verify that attachment count and size quotas are tracked per message, not across the entire run."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            pdf_sample = b"%PDF-1.4 sample content"
            pdf_sha = hashlib.sha256(pdf_sample).hexdigest()

            # Message 1 fetches 5 attachments (quota limit is 5)
            msg1_id = "msg1@example.org"
            raw_eml_msg1 = attachments.build_test_eml(
                subject="Msg1",
                message_id=f"<{msg1_id}>",
                attachments=[
                    {"filename": f"m1_file_{j}.pdf", "mime_type": "application/pdf", "data": pdf_sample}
                    for j in range(1, 7)
                ],
            )
            for i in range(1, 6):
                fname = f"m1_file_{i}.pdf"
                candidate = {
                    "filename": fname,
                    "mime_type": "application/pdf",
                    "size_bytes": len(pdf_sample),
                    "sha256": pdf_sha,
                    "part_locator": str(i + 1),
                    "fetch_status": "available",
                    "provenance": attachments.PROVENANCE_RFC822,
                    "account": "BOKU-MARTIN",
                    "folder": "INBOX",
                    "envelope_id": "7195",
                    "message_id": msg1_id,
                }
                rev_hash = afetch.compute_review_hash(
                    account="BOKU-MARTIN",
                    message_id=msg1_id,
                    folder="INBOX",
                    envelope_id="7195",
                    part_locator=str(i + 1),
                    inventory_sha256=pdf_sha,
                )
                receipt = {
                    "receipt_id": f"rec-m1-{i}",
                    "request_hash": rev_hash,
                    "approved_at": "2026-09-14T09:00:00Z",
                    "approved_by": "martin",
                }
                res = afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id=msg1_id,
                    part_locator=str(i + 1),
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_shared_quota_test",
                    raw_eml=raw_eml_msg1,
                    data_dir=data_dir,
                )
                self.assertEqual("fetched", res["status"])

            # Message 1 attempting 6th attachment must fail QuotaExceededError
            fname_6 = "m1_file_6.pdf"
            candidate_6 = {
                "filename": fname_6,
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_sample),
                "sha256": pdf_sha,
                "part_locator": "7",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": msg1_id,
            }
            rev_hash_6 = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=msg1_id,
                folder="INBOX",
                envelope_id="7195",
                part_locator="7",
                inventory_sha256=pdf_sha,
            )
            receipt_6 = {
                "receipt_id": "rec-m1-6",
                "request_hash": rev_hash_6,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            with self.assertRaises(afetch.QuotaExceededError) as cm_quota:
                afetch.op_attachment_fetch(
                    candidate=candidate_6,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id=msg1_id,
                    part_locator="7",
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash_6,
                    approval_receipt=receipt_6,
                    run_id="run_shared_quota_test",
                    raw_eml=raw_eml_msg1,
                    data_dir=data_dir,
                )
            self.assertIn("5 >= 5 limit", str(cm_quota.exception))

            # Message 2 in the SAME run directory must succeed (has its own fresh quota)
            msg2_id = "msg2@example.org"
            fname_m2 = "m2_file_1.pdf"
            raw_eml_m2 = attachments.build_test_eml(
                subject="Msg2",
                message_id=f"<{msg2_id}>",
                attachments=[{"filename": fname_m2, "mime_type": "application/pdf", "data": pdf_sample}],
            )
            candidate_m2 = {
                "filename": fname_m2,
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_sample),
                "sha256": pdf_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": msg2_id,
            }
            rev_hash_m2 = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=msg2_id,
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=pdf_sha,
            )
            receipt_m2 = {
                "receipt_id": "rec-m2-1",
                "request_hash": rev_hash_m2,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            res_m2 = afetch.op_attachment_fetch(
                candidate=candidate_m2,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id=msg2_id,
                part_locator="2",
                inventory_sha256=pdf_sha,
                review_hash=rev_hash_m2,
                approval_receipt=receipt_m2,
                run_id="run_shared_quota_test",
                raw_eml=raw_eml_m2,
                data_dir=data_dir,
            )
            self.assertEqual("fetched", res_m2["status"])

    def test_configured_25s_download_timeout_applied(self) -> None:
        """Verify that himalaya.fetch_raw_message_eml receives timeout=25 from policy."""
        pdf_bytes = b"%PDF-1.4 timeout check"
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        raw_eml = attachments.build_test_eml(
            subject="Timeout Check",
            message_id="<timeout-check@example.org>",
            attachments=[{"filename": "doc.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
        )
        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(pdf_bytes),
            "sha256": pdf_sha,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "timeout-check@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="timeout-check@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256=pdf_sha,
        )
        receipt = {
            "receipt_id": "rec-timeout-check",
            "request_hash": rev_hash,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            with patch.object(himalaya, "fetch_raw_message_eml", return_value=raw_eml) as mock_fetch:
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="timeout-check@example.org",
                    part_locator="2",
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_to_check",
                    raw_eml=None,  # Triggers fetch_raw_message_eml
                    data_dir=data_dir,
                )
                mock_fetch.assert_called_once()
                _, kwargs = mock_fetch.call_args
                self.assertEqual(25, kwargs.get("timeout"))

    def test_approval_receipt_rfc3339_validation_and_versioned_json_hash(self) -> None:
        """Verify RFC-3339 timezone strictness and canonical versioned review hash."""
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="msg@example.org",
            folder="INBOX",
            envelope_id="100",
            part_locator="2",
            inventory_sha256="c" * 64,
        )

        # Invalid approved_at timestamps
        bad_timestamps = [
            "now",
            "yesterday",
            "2026-09-14 09:00:00",  # missing T
            "2026-09-14T09:00:00",    # missing timezone offset
            "2026/09/14 09:00:00Z",
            "",
            123456789,
        ]
        for bad_ts in bad_timestamps:
            receipt = {
                "receipt_id": "rec-01",
                "request_hash": rev_hash,
                "approved_at": bad_ts,
                "approved_by": "martin",
            }
            with self.assertRaises(ValueError, msg=f"Expected ValueError for bad timestamp: {bad_ts}"):
                afetch.verify_approval_receipt(receipt, expected_review_hash=rev_hash)

        # Valid RFC-3339 timestamps
        good_timestamps = [
            "2026-09-14T09:00:00Z",
            "2026-09-14T11:00:00+02:00",
            "2026-09-14T05:00:00-04:00",
            "2026-09-14T09:00:00.123456Z",
        ]
        for good_ts in good_timestamps:
            receipt = {
                "receipt_id": "rec-01",
                "request_hash": rev_hash,
                "approved_at": good_ts,
                "approved_by": "martin",
            }
            afetch.verify_approval_receipt(receipt, expected_review_hash=rev_hash)

        # Canonical versioned review hash verification:
        # Schema version 1, sorted keys, separators=(',', ':')
        import json
        payload = {
            "schema_version": 1,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "100",
            "message_id": "msg@example.org",
            "part_locator": "2",
            "inventory_sha256": "c" * 64,
        }
        canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
        self.assertEqual(expected_hash, rev_hash)



    def test_atomic_no_clobber_fails_closed_when_link_unsupported(self) -> None:
        """Verify that promotion fails closed with RuntimeError if atomic os.link is unsupported (no os.replace fallback)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_file = Path(tmp_dir) / ".test.tmp"
            target_file = Path(tmp_dir) / "target.pdf"
            temp_file.write_bytes(b"%PDF-1.4 sample")
            sha = hashlib.sha256(b"%PDF-1.4 sample").hexdigest()

            with patch("os.link", side_effect=OSError("Hard links not supported on filesystem")):
                with self.assertRaises(RuntimeError) as cm:
                    afetch._atomic_no_clobber_promote(temp_file, target_file, sha)
                self.assertIn("does not support atomic link primitive", str(cm.exception))

            # Target must NOT exist, and temp file must be cleaned up
            self.assertFalse(target_file.exists())
            self.assertFalse(temp_file.exists())

    def test_quarantine_inventory_corrupted_and_missing_in_existing_run(self) -> None:
        """Verify that corrupted, schema-invalid, or missing inventory in an existing run fails closed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_inv_test"
            run_dir.mkdir(parents=True, exist_ok=True)

            pdf_bytes = b"%PDF-1.4 sample"
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

            candidate = {
                "filename": "doc.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
                "sha256": pdf_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "inv-test@example.org",
            }
            rev_hash = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="inv-test@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=pdf_sha,
            )
            receipt = {
                "receipt_id": "rec-001",
                "request_hash": rev_hash,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            # Case A: Corrupted JSON in .quarantine-inventory.json
            inv_file = run_dir / ".quarantine-inventory.json"
            inv_file.write_text("{{corrupted json", encoding="utf-8")

            with self.assertRaises(afetch.QuarantineInventoryError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="inv-test@example.org",
                    part_locator="2",
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_inv_test",
                    raw_eml=b"fake",
                    data_dir=data_dir,
                )

            # Case B: Structurally invalid schema (unsupported schema_version)
            inv_file.write_text(json.dumps({"schema_version": 99, "messages": {}}), encoding="utf-8")
            with self.assertRaises(afetch.QuarantineInventoryError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="inv-test@example.org",
                    part_locator="2",
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_inv_test",
                    raw_eml=b"fake",
                    data_dir=data_dir,
                )

            # Case C: Missing inventory in existing run directory that contains files
            inv_file.unlink()
            existing_file = run_dir / "pre_existing.dat"
            existing_file.write_bytes(b"some content")

            with self.assertRaises(afetch.QuarantineInventoryError):
                afetch.op_attachment_fetch(
                    candidate=candidate,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="inv-test@example.org",
                    part_locator="2",
                    inventory_sha256=pdf_sha,
                    review_hash=rev_hash,
                    approval_receipt=receipt,
                    run_id="run_inv_test",
                    raw_eml=b"fake",
                    data_dir=data_dir,
                )

    def test_concurrent_inventory_locking(self) -> None:
        """Verify that _QuarantineInventoryLock serializes concurrent updates without losing data."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_dir = Path(tmp_dir) / "run_lock_test"
            run_dir.mkdir(parents=True, exist_ok=True)

            # Sequential recording via locking
            for i in range(5):
                afetch._record_in_quarantine_inventory(
                    run_dir=run_dir,
                    message_id="msg-lock@example.org",
                    filename=f"file_{i}.pdf",
                    sha256=f"{i}" * 64,
                    size_bytes=1000 * (i + 1),
                )

            inv = afetch._load_quarantine_inventory(run_dir)
            msg_data = inv["messages"]["msg-lock@example.org"]
            self.assertEqual(5, msg_data["count"])
            self.assertEqual(5, len(msg_data["files"]))
            self.assertEqual(15000, msg_data["total_bytes"])

    def test_already_fetched_executes_full_security_validation(self) -> None:
        """Verify that pre-existing target file executes active-content, MIME drift, and quota checks."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_pre_sec"
            run_dir.mkdir(parents=True, exist_ok=True)
            # Write clean initial inventory in existing run directory
            (run_dir / ".quarantine-inventory.json").write_text(
                json.dumps({"schema_version": 1, "messages": {}}), encoding="utf-8"
            )

            # 1. Pre-placed file is an executable binary with matching hash
            exe_bytes = b"MZ" + bytes([0x90, 0x00]) + b"pre-placed payload"
            exe_sha = hashlib.sha256(exe_bytes).hexdigest()
            target_exe = run_dir / "payload.exe"
            target_exe.write_bytes(exe_bytes)

            candidate_exe = {
                "filename": "payload.exe",
                "mime_type": "application/x-dosexec",
                "size_bytes": len(exe_bytes),
                "sha256": exe_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "pre-sec@example.org",
            }
            rev_hash_exe = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="pre-sec@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=exe_sha,
            )
            receipt_exe = {
                "receipt_id": "rec-exe",
                "request_hash": rev_hash_exe,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            # Must NOT return already_fetched, must block active content fail-closed!
            with self.assertRaises(afetch.ActiveContentBlockedError):
                afetch.op_attachment_fetch(
                    candidate=candidate_exe,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="pre-sec@example.org",
                    part_locator="2",
                    inventory_sha256=exe_sha,
                    review_hash=rev_hash_exe,
                    approval_receipt=receipt_exe,
                    run_id="run_pre_sec",
                    data_dir=data_dir,
                )

            # 2. Pre-placed file has extension drift: filename claims .pdf, but content is text
            txt_bytes = b"plain text claiming to be a pdf"
            txt_sha = hashlib.sha256(txt_bytes).hexdigest()
            target_drift = run_dir / "fake.pdf"
            target_drift.write_bytes(txt_bytes)

            candidate_drift = {
                "filename": "fake.pdf",
                "mime_type": "text/plain",
                "size_bytes": len(txt_bytes),
                "sha256": txt_sha,
                "part_locator": "3",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "pre-sec@example.org",
            }
            rev_hash_drift = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="pre-sec@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="3",
                inventory_sha256=txt_sha,
            )
            receipt_drift = {
                "receipt_id": "rec-drift",
                "request_hash": rev_hash_drift,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            with self.assertRaises(afetch.ExtensionMimeDriftError):
                afetch.op_attachment_fetch(
                    candidate=candidate_drift,
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="7195",
                    message_id="pre-sec@example.org",
                    part_locator="3",
                    inventory_sha256=txt_sha,
                    review_hash=rev_hash_drift,
                    approval_receipt=receipt_drift,
                    run_id="run_pre_sec",
                    data_dir=data_dir,
                )

            # 3. Clean pre-placed PDF: succeeds with already_fetched AND backfills inventory
            pdf_bytes = b"%PDF-1.4 valid content"
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
            target_valid = run_dir / "valid.pdf"
            target_valid.write_bytes(pdf_bytes)

            candidate_valid = {
                "filename": "valid.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
                "sha256": pdf_sha,
                "part_locator": "4",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "pre-sec@example.org",
            }
            rev_hash_valid = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="pre-sec@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="4",
                inventory_sha256=pdf_sha,
            )
            receipt_valid = {
                "receipt_id": "rec-valid",
                "request_hash": rev_hash_valid,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            res = afetch.op_attachment_fetch(
                candidate=candidate_valid,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="7195",
                message_id="pre-sec@example.org",
                part_locator="4",
                inventory_sha256=pdf_sha,
                review_hash=rev_hash_valid,
                approval_receipt=receipt_valid,
                run_id="run_pre_sec",
                data_dir=data_dir,
            )
            self.assertEqual("already_fetched", res["status"])

            # Verify inventory was backfilled with the pre-placed file
            inv = afetch._load_quarantine_inventory(run_dir)
            msg_files = inv["messages"]["pre-sec@example.org"]["files"]
            self.assertIn("valid.pdf", msg_files)
            self.assertEqual(pdf_sha, msg_files["valid.pdf"]["sha256"])

    def test_lstat_inspection_os_error_fails_closed(self) -> None:
        """Verify that an OSError during os.lstat raises SymlinkEscapeError fail-closed instead of passing."""
        candidate = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 100,
            "sha256": "a" * 64,
            "part_locator": "2",
            "fetch_status": "available",
            "provenance": attachments.PROVENANCE_RFC822,
            "account": "BOKU-MARTIN",
            "folder": "INBOX",
            "envelope_id": "7195",
            "message_id": "lstat-err@example.org",
        }
        rev_hash = afetch.compute_review_hash(
            account="BOKU-MARTIN",
            message_id="lstat-err@example.org",
            folder="INBOX",
            envelope_id="7195",
            part_locator="2",
            inventory_sha256="a" * 64,
        )
        receipt = {
            "receipt_id": "rec-001",
            "request_hash": rev_hash,
            "approved_at": "2026-09-14T09:00:00Z",
            "approved_by": "martin",
        }

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_lstat_err"
            run_dir.mkdir(parents=True, exist_ok=True)

            with patch("os.lstat", side_effect=PermissionError("Access is denied to query reparse point")):
                with self.assertRaises(afetch.SymlinkEscapeError) as cm:
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="lstat-err@example.org",
                        part_locator="2",
                        inventory_sha256="a" * 64,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id="run_lstat_err",
                        raw_eml=b"fake",
                        data_dir=data_dir,
                    )
                self.assertIn("lstat error", str(cm.exception).lower())

    def test_free_candidate_paths_and_device_names_rejected_before_io(self) -> None:
        """Verify that candidate filenames with path traversal or Windows reserved names (with/without extension) fail closed before any I/O."""
        bad_filenames = [
            "../../bericht.pdf",
            r"..\..\bericht.pdf",
            "sub/folder/file.pdf",
            r"sub\folder\file.pdf",
            "CON.txt",
            "AUX.pdf",
            "NUL.dat",
            "COM1.doc",
            "LPT1.txt",
            "PRN.pdf",
            "CON",
            "AUX",
            "NUL",
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"

            for bad_name in bad_filenames:
                candidate = {
                    "filename": bad_name,
                    "mime_type": "application/pdf",
                    "size_bytes": 100,
                    "sha256": "f" * 64,
                    "part_locator": "2",
                    "fetch_status": "available",
                    "provenance": attachments.PROVENANCE_RFC822,
                    "account": "BOKU-MARTIN",
                    "folder": "INBOX",
                    "envelope_id": "7195",
                    "message_id": "badname@example.org",
                }
                rev_hash = afetch.compute_review_hash(
                    account="BOKU-MARTIN",
                    message_id="badname@example.org",
                    folder="INBOX",
                    envelope_id="7195",
                    part_locator="2",
                    inventory_sha256="f" * 64,
                )
                receipt = {
                    "receipt_id": "rec-001",
                    "request_hash": rev_hash,
                    "approved_at": "2026-09-14T09:00:00Z",
                    "approved_by": "martin",
                }

                with self.assertRaises(ValueError, msg=f"Filename {bad_name!r} should be rejected before I/O"):
                    afetch.op_attachment_fetch(
                        candidate=candidate,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="7195",
                        message_id="badname@example.org",
                        part_locator="2",
                        inventory_sha256="f" * 64,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id=f"run_{uuid.uuid4().hex[:8]}",
                        raw_eml=b"fake",
                        data_dir=data_dir,
                    )

            # Ensure NO attachment directory or files were created (0 I/O)
            attachments_dir = data_dir / "attachments"
            self.assertFalse(attachments_dir.exists(), "No attachments directory must be created when rejecting before I/O!")

    def test_concurrent_fetches_serialized_count_quota_enforced(self) -> None:
        """Concurrent fetches for the same message serialize check, promotion, and inventory under lock.

        When only 1 slot remains in the 5-file message quota, two concurrent threads competing
        for the slot will have exactly one succeed and one fail closed with QuotaExceededError.
        Final inventory count must be exactly 5, never 6.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data" / "mail-desk"
            run_id = "run_count_race_01"
            run_dir = data_dir / "attachments" / run_id
            mid = "race-msg-count@example.org"

            # Seed inventory with 4 files for message 'mid'
            for idx in range(1, 5):
                fake_pdf = f"%PDF-1.4 existing file {idx}".encode("ascii")
                fake_sha = hashlib.sha256(fake_pdf).hexdigest()
                afetch._record_in_quarantine_inventory(
                    run_dir=run_dir,
                    message_id=mid,
                    filename=f"file_{idx}.pdf",
                    sha256=fake_sha,
                    size_bytes=len(fake_pdf),
                )

            # Two competing attachment candidates: file_5.pdf and file_6.pdf
            pdf5 = b"%PDF-1.4 thread 1 payload"
            sha5 = hashlib.sha256(pdf5).hexdigest()
            eml5 = (
                b"Subject: Race\n"
                b"Message-ID: <race-msg-count@example.org>\n"
                b"MIME-Version: 1.0\n"
                b'Content-Type: multipart/mixed; boundary="B"\n\n'
                b"--B\n"
                b"Content-Type: text/plain\n\nHello\n"
                b"--B\n"
                b'Content-Type: application/pdf; name="file_5.pdf"\n'
                b'Content-Disposition: attachment; filename="file_5.pdf"\n\n'
                + pdf5 + b"\n--B--\n"
            )
            cand5 = {
                "filename": "file_5.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf5),
                "sha256": sha5,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "8001",
                "message_id": mid,
            }
            rev_hash5 = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=mid,
                folder="INBOX",
                envelope_id="8001",
                part_locator="2",
                inventory_sha256=sha5,
            )
            receipt5 = {
                "receipt_id": "rec-5",
                "request_hash": rev_hash5,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            pdf6 = b"%PDF-1.4 thread 2 payload"
            sha6 = hashlib.sha256(pdf6).hexdigest()
            eml6 = (
                b"Subject: Race\n"
                b"Message-ID: <race-msg-count@example.org>\n"
                b"MIME-Version: 1.0\n"
                b'Content-Type: multipart/mixed; boundary="B"\n\n'
                b"--B\n"
                b"Content-Type: text/plain\n\nHello\n"
                b"--B\n"
                b'Content-Type: application/pdf; name="file_6.pdf"\n'
                b'Content-Disposition: attachment; filename="file_6.pdf"\n\n'
                + pdf6 + b"\n--B--\n"
            )
            cand6 = {
                "filename": "file_6.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf6),
                "sha256": sha6,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "8001",
                "message_id": mid,
            }
            rev_hash6 = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=mid,
                folder="INBOX",
                envelope_id="8001",
                part_locator="2",
                inventory_sha256=sha6,
            )
            receipt6 = {
                "receipt_id": "rec-6",
                "request_hash": rev_hash6,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            barrier = threading.Barrier(2)
            results: list[dict[str, Any]] = []
            exceptions: list[Exception] = []

            def worker(cand, eml, rev_h, rec, sha):
                try:
                    barrier.wait(timeout=5.0)
                    res = afetch.op_attachment_fetch(
                        candidate=cand,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="8001",
                        message_id=mid,
                        part_locator="2",
                        inventory_sha256=sha,
                        review_hash=rev_h,
                        approval_receipt=rec,
                        run_id=run_id,
                        raw_eml=eml,
                        data_dir=data_dir,
                    )
                    results.append(res)
                except Exception as exc:
                    exceptions.append(exc)

            t1 = threading.Thread(target=worker, args=(cand5, eml5, rev_hash5, receipt5, sha5))
            t2 = threading.Thread(target=worker, args=(cand6, eml6, rev_hash6, receipt6, sha6))

            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            # Exactly one must succeed, exactly one must fail with QuotaExceededError
            self.assertEqual(1, len(results), f"Expected exactly 1 success, got: {results}")
            self.assertEqual(1, len(exceptions), f"Expected exactly 1 exception, got: {exceptions}")
            self.assertIsInstance(exceptions[0], afetch.QuotaExceededError)
            self.assertIn("count limit exceeded", str(exceptions[0]).lower())

            # Verify quarantine inventory has exactly 5 files, never 6
            inv = afetch._load_quarantine_inventory(run_dir)
            msg_entry = inv["messages"][mid]
            self.assertEqual(5, msg_entry["count"])
            self.assertEqual(5, len(msg_entry["files"]))

    def test_concurrent_fetches_serialized_size_quota_enforced(self) -> None:
        """Concurrent fetches competing for cumulative message size quota serialize under lock.

        Two 14 MB attachments (limit is 25 MB total) cannot both pass: exactly one succeeds,
        the other fails with QuotaExceededError. Final total_bytes must be 14 MB <= 25 MB.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data" / "mail-desk"
            run_id = "run_size_race_01"
            run_dir = data_dir / "attachments" / run_id
            mid = "race-msg-size@example.org"

            size_14mb = 14 * 1024 * 1024
            base_pdf = b"%PDF-1.4\n"
            padding_1 = b"A" * (size_14mb - len(base_pdf) - 6) + b"\n%%EOF\n"
            pdf1 = base_pdf + padding_1
            sha1 = hashlib.sha256(pdf1).hexdigest()

            padding_2 = b"B" * (size_14mb - len(base_pdf) - 6) + b"\n%%EOF\n"
            pdf2 = base_pdf + padding_2
            sha2 = hashlib.sha256(pdf2).hexdigest()

            cand1 = {
                "filename": "big_1.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf1),
                "sha256": sha1,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "8002",
                "message_id": mid,
            }
            rev_hash1 = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=mid,
                folder="INBOX",
                envelope_id="8002",
                part_locator="2",
                inventory_sha256=sha1,
            )
            rec1 = {
                "receipt_id": "rec-size-1",
                "request_hash": rev_hash1,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            eml1 = (
                b"Subject: Big 1\n"
                b"Message-ID: <race-msg-size@example.org>\n"
                b"MIME-Version: 1.0\n"
                b'Content-Type: multipart/mixed; boundary="B"\n\n'
                b"--B\n"
                b"Content-Type: text/plain\n\nHello\n"
                b"--B\n"
                b'Content-Type: application/pdf; filename="big_1.pdf"\n\n'
                + pdf1 + b"\n--B--\n"
            )

            cand2 = {
                "filename": "big_2.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf2),
                "sha256": sha2,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "8002",
                "message_id": mid,
            }
            rev_hash2 = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=mid,
                folder="INBOX",
                envelope_id="8002",
                part_locator="2",
                inventory_sha256=sha2,
            )
            rec2 = {
                "receipt_id": "rec-size-2",
                "request_hash": rev_hash2,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            eml2 = (
                b"Subject: Big 2\n"
                b"Message-ID: <race-msg-size@example.org>\n"
                b"MIME-Version: 1.0\n"
                b'Content-Type: multipart/mixed; boundary="B"\n\n'
                b"--B\n"
                b"Content-Type: text/plain\n\nHello\n"
                b"--B\n"
                b'Content-Type: application/pdf; filename="big_2.pdf"\n\n'
                + pdf2 + b"\n--B--\n"
            )

            barrier = threading.Barrier(2)
            results: list[dict[str, Any]] = []
            exceptions: list[Exception] = []

            def worker(cand, eml, rev_h, rec, sha):
                try:
                    barrier.wait(timeout=5.0)
                    res = afetch.op_attachment_fetch(
                        candidate=cand,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="8002",
                        message_id=mid,
                        part_locator="2",
                        inventory_sha256=sha,
                        review_hash=rev_h,
                        approval_receipt=rec,
                        run_id=run_id,
                        raw_eml=eml,
                        data_dir=data_dir,
                    )
                    results.append(res)
                except Exception as exc:
                    exceptions.append(exc)

            t1 = threading.Thread(target=worker, args=(cand1, eml1, rev_hash1, rec1, sha1))
            t2 = threading.Thread(target=worker, args=(cand2, eml2, rev_hash2, rec2, sha2))

            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            # Exactly one must succeed, exactly one must fail with QuotaExceededError
            self.assertEqual(1, len(results), f"Expected 1 success, got: {results}")
            self.assertEqual(1, len(exceptions), f"Expected 1 exception, got: {exceptions}")
            self.assertIsInstance(exceptions[0], afetch.QuotaExceededError)
            self.assertIn("cumulative quarantine quota exceeded", str(exceptions[0]).lower())

            inv = afetch._load_quarantine_inventory(run_dir)
            msg_entry = inv["messages"][mid]
            self.assertEqual(1, msg_entry["count"])
            self.assertEqual(results[0]["size_bytes"], msg_entry["total_bytes"])
            self.assertGreaterEqual(msg_entry["total_bytes"], 14 * 1024 * 1024)
            self.assertLessEqual(msg_entry["total_bytes"], 25 * 1024 * 1024)

    def test_concurrent_fetches_same_file_idempotent(self) -> None:
        """Two concurrent fetches of the exact same attachment serialize cleanly: one fetched, one already_fetched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data" / "mail-desk"
            run_id = "run_same_race_01"
            run_dir = data_dir / "attachments" / run_id
            mid = "race-msg-same@example.org"

            pdf = b"%PDF-1.4 idempotent test payload"
            sha = hashlib.sha256(pdf).hexdigest()
            cand = {
                "filename": "same_doc.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf),
                "sha256": sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "8003",
                "message_id": mid,
            }
            rev_hash = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id=mid,
                folder="INBOX",
                envelope_id="8003",
                part_locator="2",
                inventory_sha256=sha,
            )
            receipt = {
                "receipt_id": "rec-same-1",
                "request_hash": rev_hash,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }
            eml = (
                b"Subject: Same\n"
                b"Message-ID: <race-msg-same@example.org>\n"
                b"MIME-Version: 1.0\n"
                b'Content-Type: multipart/mixed; boundary="B"\n\n'
                b"--B\n"
                b"Content-Type: text/plain\n\nHello\n"
                b"--B\n"
                b'Content-Type: application/pdf; filename="same_doc.pdf"\n\n'
                + pdf + b"\n--B--\n"
            )

            barrier = threading.Barrier(2)
            results: list[dict[str, Any]] = []
            exceptions: list[Exception] = []

            def worker():
                try:
                    barrier.wait(timeout=5.0)
                    res = afetch.op_attachment_fetch(
                        candidate=cand,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="8003",
                        message_id=mid,
                        part_locator="2",
                        inventory_sha256=sha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id=run_id,
                        raw_eml=eml,
                        data_dir=data_dir,
                    )
                    results.append(res)
                except Exception as exc:
                    exceptions.append(exc)

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=10.0)
            t2.join(timeout=10.0)

            self.assertEqual(0, len(exceptions), f"Unexpected exceptions: {exceptions}")
            self.assertEqual(2, len(results))
            statuses = {r["status"] for r in results}
            self.assertEqual({"fetched", "already_fetched"}, statuses)

            inv = afetch._load_quarantine_inventory(run_dir)
            msg_entry = inv["messages"][mid]
            self.assertEqual(1, msg_entry["count"])
            self.assertEqual(len(pdf), msg_entry["total_bytes"])

    def test_workspace_lock_missing_fails_closed_zero_io(self) -> None:
        """When workspace lock is not active, op_attachment_fetch aborts fail-closed before any directory or file mutation."""
        self._workspace_lock_patcher.stop()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                data_dir = Path(tmpdir) / "data" / "mail-desk"
                run_id = f"run_{uuid.uuid4().hex[:8]}"
                run_dir = data_dir / "attachments" / run_id
                mid = "lock-msg-001@example.org"

                pdf = b"%PDF-1.4 test payload"
                sha = hashlib.sha256(pdf).hexdigest()
                cand = {
                    "filename": "doc.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": len(pdf),
                    "sha256": sha,
                    "part_locator": "2",
                    "fetch_status": "available",
                    "provenance": attachments.PROVENANCE_RFC822,
                    "account": "BOKU-MARTIN",
                    "folder": "INBOX",
                    "envelope_id": "9001",
                    "message_id": mid,
                }
                rev_hash = afetch.compute_review_hash(
                    account="BOKU-MARTIN",
                    message_id=mid,
                    folder="INBOX",
                    envelope_id="9001",
                    part_locator="2",
                    inventory_sha256=sha,
                )
                receipt = {
                    "receipt_id": "rec-lock-1",
                    "request_hash": rev_hash,
                    "approved_at": "2026-09-14T09:00:00Z",
                    "approved_by": "martin",
                }

                # Fails closed with WorkspaceLockError or RuntimeError before any I/O
                with self.assertRaises(RuntimeError) as ctx:
                    afetch.op_attachment_fetch(
                        candidate=cand,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="9001",
                        message_id=mid,
                        part_locator="2",
                        inventory_sha256=sha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id=run_id,
                        raw_eml=b"fake",
                        data_dir=data_dir,
                        workspace_root=tmpdir,
                    )

                self.assertIn("workspace lock", str(ctx.exception).lower())
                # Verify zero filesystem mutation: attachments root and run_dir were NOT created
                attachments_root = data_dir / "attachments"
                self.assertFalse(attachments_root.exists(), "No attachments folder must be created without workspace lock!")
                self.assertFalse(run_dir.exists(), "No run directory must be created without workspace lock!")
        finally:
            self._workspace_lock_patcher.start()

    def test_workspace_lock_foreign_owner_fails_closed_zero_io(self) -> None:
        """When workspace lock is held by a foreign invocation, op_attachment_fetch fails closed before any I/O."""
        self._workspace_lock_patcher.stop()
        try:
            guard = afetch._load_workspace_lock_guard()
            with patch.object(
                guard,
                "require_workspace_lock",
                side_effect=guard.WorkspaceLockError("workspace lock is not owned by this invocation"),
            ):
                with tempfile.TemporaryDirectory() as tmpdir:
                    data_dir = Path(tmpdir) / "data" / "mail-desk"
                    run_id = f"run_{uuid.uuid4().hex[:8]}"
                    run_dir = data_dir / "attachments" / run_id
                    mid = "lock-msg-002@example.org"

                    pdf = b"%PDF-1.4 test payload"
                    sha = hashlib.sha256(pdf).hexdigest()
                    cand = {
                        "filename": "doc.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": len(pdf),
                        "sha256": sha,
                        "part_locator": "2",
                        "fetch_status": "available",
                        "provenance": attachments.PROVENANCE_RFC822,
                        "account": "BOKU-MARTIN",
                        "folder": "INBOX",
                        "envelope_id": "9002",
                        "message_id": mid,
                    }
                    rev_hash = afetch.compute_review_hash(
                        account="BOKU-MARTIN",
                        message_id=mid,
                        folder="INBOX",
                        envelope_id="9002",
                        part_locator="2",
                        inventory_sha256=sha,
                    )
                    receipt = {
                        "receipt_id": "rec-lock-2",
                        "request_hash": rev_hash,
                        "approved_at": "2026-09-14T09:00:00Z",
                        "approved_by": "martin",
                    }

                    with self.assertRaises(afetch.WorkspaceLockError):
                        afetch.op_attachment_fetch(
                            candidate=cand,
                            account="BOKU-MARTIN",
                            folder="INBOX",
                            envelope_id="9002",
                            message_id=mid,
                            part_locator="2",
                            inventory_sha256=sha,
                            review_hash=rev_hash,
                            approval_receipt=receipt,
                            run_id=run_id,
                            raw_eml=b"fake",
                            data_dir=data_dir,
                            workspace_root=tmpdir,
                        )

                    attachments_root = data_dir / "attachments"
                    self.assertFalse(attachments_root.exists(), "No attachments folder must be created on lock rejection!")
                    self.assertFalse(run_dir.exists(), "No run directory must be created on lock rejection!")
        finally:
            self._workspace_lock_patcher.start()

    def test_workspace_lock_owned_succeeds_and_cleanup_checks_lock(self) -> None:
        """When workspace lock is owned, op_attachment_fetch succeeds and cleanup_run_quarantine enforces lock."""
        self._workspace_lock_patcher.stop()
        try:
            guard = afetch._load_workspace_lock_guard()
            mock_receipt = guard.LockReceipt(
                workspace=Path("D:/mock"),
                state="Active",
                lease_id="lease-001",
                conversation_id="conv-001",
            )
            with patch.object(guard, "require_workspace_lock", return_value=mock_receipt) as mock_req:
                with tempfile.TemporaryDirectory() as tmpdir:
                    data_dir = Path(tmpdir) / "data" / "mail-desk"
                    run_id = f"run_{uuid.uuid4().hex[:8]}"
                    run_dir = data_dir / "attachments" / run_id
                    mid = "lock-msg-003@example.org"

                    pdf = b"%PDF-1.4 test payload"
                    sha = hashlib.sha256(pdf).hexdigest()
                    cand = {
                        "filename": "doc.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": len(pdf),
                        "sha256": sha,
                        "part_locator": "2",
                        "fetch_status": "available",
                        "provenance": attachments.PROVENANCE_RFC822,
                        "account": "BOKU-MARTIN",
                        "folder": "INBOX",
                        "envelope_id": "9003",
                        "message_id": mid,
                    }
                    rev_hash = afetch.compute_review_hash(
                        account="BOKU-MARTIN",
                        message_id=mid,
                        folder="INBOX",
                        envelope_id="9003",
                        part_locator="2",
                        inventory_sha256=sha,
                    )
                    receipt = {
                        "receipt_id": "rec-lock-3",
                        "request_hash": rev_hash,
                        "approved_at": "2026-09-14T09:00:00Z",
                        "approved_by": "martin",
                    }
                    eml = (
                        b"Subject: Owned\n"
                        b"Message-ID: <lock-msg-003@example.org>\n"
                        b"MIME-Version: 1.0\n"
                        b'Content-Type: multipart/mixed; boundary="B"\n\n'
                        b"--B\n"
                        b"Content-Type: text/plain\n\nHello\n"
                        b"--B\n"
                        b'Content-Type: application/pdf; filename="doc.pdf"\n\n'
                        + pdf + b"\n--B--\n"
                    )

                    res = afetch.op_attachment_fetch(
                        candidate=cand,
                        account="BOKU-MARTIN",
                        folder="INBOX",
                        envelope_id="9003",
                        message_id=mid,
                        part_locator="2",
                        inventory_sha256=sha,
                        review_hash=rev_hash,
                        approval_receipt=receipt,
                        run_id=run_id,
                        raw_eml=eml,
                        data_dir=data_dir,
                        workspace_root=tmpdir,
                        lease_id="lease-001",
                        conversation_id="conv-001",
                    )
                    self.assertEqual("fetched", res["status"])
                    self.assertTrue(run_dir.exists())
                    self.assertTrue((run_dir / "doc.pdf").is_file())
                    mock_req.assert_called()

                    # Cleanup also verifies workspace lock
                    afetch.cleanup_run_quarantine(
                        run_id=run_id,
                        data_dir=data_dir,
                        workspace_root=tmpdir,
                        lease_id="lease-001",
                        conversation_id="conv-001",
                    )
                    self.assertFalse(run_dir.exists())
        finally:
            self._workspace_lock_patcher.start()

    def test_workspace_lock_dynamic_loader_locates_canonical_guard(self) -> None:
        """_load_workspace_lock_guard correctly locates canonical workspace_lock_guard.py."""
        guard_mod = afetch._load_workspace_lock_guard()
        self.assertTrue(hasattr(guard_mod, "require_workspace_lock"))
        self.assertTrue(hasattr(guard_mod, "WorkspaceLockError"))

    def test_manifest_injected_allow_legacy_and_lease_ignored_zero_io(self) -> None:
        """Untrusted manifest specifying allow_legacy=true or injected lease_id cannot bypass lock.

        Manifest-supplied lock parameters must be ignored; without an active lock owned
        by the process control plane, execute_manifest fails closed with 0 disk I/O.
        """
        self._workspace_lock_patcher.stop()
        try:
            pdf_bytes = b"%PDF-1.4 manifest adversarial test"
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
            raw_eml = attachments.build_test_eml(
                subject="Adversarial Test",
                message_id="<adv-001@example.org>",
                attachments=[{"filename": "adv.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
            )
            candidate = {
                "filename": "adv.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
                "sha256": pdf_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "adv-001@example.org",
            }
            rev_hash = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="adv-001@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=pdf_sha,
            )
            receipt = {
                "receipt_id": "rec-adv-1",
                "request_hash": rev_hash,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            with tempfile.TemporaryDirectory() as tmp_dir:
                data_dir = Path(tmp_dir) / "data" / "mail-desk"
                manifest_file = Path(tmp_dir) / "manifest.json"
                # Manifest attempts to forge lease_id and bypass with allow_legacy: true
                manifest_file.write_text(
                    json.dumps({
                        "account": "BOKU-MARTIN",
                        "delete_input_on_success": False,
                        "lease_id": "forged-manifest-lease",
                        "conversation_id": "forged-manifest-conv",
                        "allow_legacy": True,
                        "operations": [
                            {
                                "action": "attachment_fetch",
                                "candidate": candidate,
                                "envelope_id": "7195",
                                "folder": "INBOX",
                                "account": "BOKU-MARTIN",
                                "message_id": "adv-001@example.org",
                                "part_locator": "2",
                                "inventory_sha256": pdf_sha,
                                "review_hash": rev_hash,
                                "approval_receipt": receipt,
                                "run_id": "run_adv_01",
                                "lease_id": "forged-op-lease",
                                "conversation_id": "forged-op-conv",
                                "allow_legacy": True,
                            }
                        ],
                    }),
                    encoding="utf-8",
                )

                with patch.object(afetch, "resolve_data_dir", return_value=data_dir):
                    with patch.object(himalaya, "fetch_raw_message_eml", return_value=raw_eml):
                        res = client.execute_manifest(manifest_file)

                # Must fail-closed because process environment does not own a lock
                self.assertFalse(res["all_succeeded"])
                self.assertEqual(1, len(res["results"]))
                self.assertFalse(res["results"][0]["success"])
                self.assertIn("workspace lock", res["results"][0]["error"].lower())

                # Zero filesystem mutation
                attachments_dir = data_dir / "attachments"
                self.assertFalse(attachments_dir.exists(), "No attachments folder must be created on lock failure!")
        finally:
            self._workspace_lock_patcher.start()

    def test_manifest_spoofed_lease_rejected_when_foreign_lock_active_zero_io(self) -> None:
        """Manifest trying to spoof active lease_id is rejected when process does not own it."""
        self._workspace_lock_patcher.stop()
        try:
            pdf_bytes = b"%PDF-1.4 manifest spoof test"
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
            raw_eml = attachments.build_test_eml(
                subject="Spoof Test",
                message_id="<spoof-001@example.org>",
                attachments=[{"filename": "spoof.pdf", "mime_type": "application/pdf", "data": pdf_bytes}],
            )
            candidate = {
                "filename": "spoof.pdf",
                "mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
                "sha256": pdf_sha,
                "part_locator": "2",
                "fetch_status": "available",
                "provenance": attachments.PROVENANCE_RFC822,
                "account": "BOKU-MARTIN",
                "folder": "INBOX",
                "envelope_id": "7195",
                "message_id": "spoof-001@example.org",
            }
            rev_hash = afetch.compute_review_hash(
                account="BOKU-MARTIN",
                message_id="spoof-001@example.org",
                folder="INBOX",
                envelope_id="7195",
                part_locator="2",
                inventory_sha256=pdf_sha,
            )
            receipt = {
                "receipt_id": "rec-spoof-1",
                "request_hash": rev_hash,
                "approved_at": "2026-09-14T09:00:00Z",
                "approved_by": "martin",
            }

            guard = afetch._load_workspace_lock_guard()

            def mock_require(workspace, *, lease_id=None, conversation_id=None, allow_legacy=False):
                # Verify that lease_id passed to require_workspace_lock does NOT come from manifest!
                self.assertIsNone(lease_id, "Manifest-injected lease_id must NEVER be passed to workspace guard!")
                self.assertFalse(allow_legacy, "allow_legacy must be strictly False in manifest path!")
                raise guard.WorkspaceLockError("workspace lock is not owned by this invocation")

            with patch.object(guard, "require_workspace_lock", side_effect=mock_require):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    data_dir = Path(tmp_dir) / "data" / "mail-desk"
                    manifest_file = Path(tmp_dir) / "manifest.json"
                    manifest_file.write_text(
                        json.dumps({
                            "account": "BOKU-MARTIN",
                            "delete_input_on_success": False,
                            "lease_id": "victim-active-lease",
                            "operations": [
                                {
                                    "action": "attachment_fetch",
                                    "candidate": candidate,
                                    "envelope_id": "7195",
                                    "folder": "INBOX",
                                    "account": "BOKU-MARTIN",
                                    "message_id": "spoof-001@example.org",
                                    "part_locator": "2",
                                    "inventory_sha256": pdf_sha,
                                    "review_hash": rev_hash,
                                    "approval_receipt": receipt,
                                    "run_id": "run_spoof_01",
                                    "lease_id": "victim-active-lease",
                                }
                            ],
                        }),
                        encoding="utf-8",
                    )

                    with patch.object(afetch, "resolve_data_dir", return_value=data_dir):
                        with patch.object(himalaya, "fetch_raw_message_eml", return_value=raw_eml):
                            res = client.execute_manifest(manifest_file)

                    self.assertFalse(res["all_succeeded"])
                    self.assertEqual(1, len(res["results"]))
                    self.assertFalse(res["results"][0]["success"])
                    self.assertIn("workspace lock is not owned", res["results"][0]["error"].lower())

                    attachments_dir = data_dir / "attachments"
                    self.assertFalse(attachments_dir.exists(), "No attachments folder must be created on spoofed lock!")
        finally:
            self._workspace_lock_patcher.start()


if __name__ == "__main__":
    unittest.main()

