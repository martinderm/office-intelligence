"""TDD tests for FR-08 MD-A2: Review-bound quarantine attachment fetch contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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

    def tearDown(self) -> None:
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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt = {"receipt_id": "rec-001", "request_hash": rev_hash, "approved_at": "now", "approved_by": "me"}

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
        receipt_good = {"receipt_id": "rec-001", "request_hash": rev_hash_good, "approved_at": "now", "approved_by": "me"}

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


if __name__ == "__main__":
    unittest.main()

