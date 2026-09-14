"""TDD tests for FR-08 MD-A1: Read-only MIME inventory and attachment policy contract."""

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
from core import classifier  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402


class MailDeskAttachmentsMDA1Tests(unittest.TestCase):
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

    def test_policy_defaults_contain_expected_thresholds(self) -> None:
        policy = attachment_policy.DEFAULT_ATTACHMENT_POLICY
        self.assertEqual("1.0.0", policy["version"])
        self.assertEqual(5, policy["transport"]["max_attachments_per_message"])
        self.assertEqual(15 * 1024 * 1024, policy["transport"]["max_single_file_bytes"])
        self.assertEqual(25 * 1024 * 1024, policy["transport"]["max_total_bytes_per_message"])
        self.assertEqual(25, policy["transport"]["download_timeout_seconds"])
        self.assertEqual(10, policy["extraction"]["pdf_max_pages"])
        self.assertEqual(15000, policy["budget"]["max_chars_per_attachment"])
        self.assertEqual(30000, policy["budget"]["max_chars_per_message"])

    def test_sanitize_filename_strips_traversal_and_windows_prefixes(self) -> None:
        self.assertEqual(
            "document.pdf",
            attachment_policy.sanitize_attachment_filename("document.pdf"),
        )
        self.assertEqual(
            "document.pdf",
            attachment_policy.sanitize_attachment_filename(r"..\..\..\document.pdf"),
        )
        self.assertEqual(
            "Programme_PolicyTalks2026.pdf",
            attachment_policy.sanitize_attachment_filename(
                r"\\?\D:\users\dagobert\himalaya\downloads\BOKU-MARTIN\Programme_PolicyTalks2026.pdf"
            ),
        )
        self.assertEqual(
            "report.pdf",
            attachment_policy.sanitize_attachment_filename("/var/tmp/../../report.pdf"),
        )
        self.assertEqual(
            "unnamed_attachment",
            attachment_policy.sanitize_attachment_filename(""),
        )

    def test_policy_checks_allowed_pdf(self) -> None:
        status, reason = attachment_policy.check_attachment_policy(
            filename="survey.pdf",
            mime_type="application/pdf",
            size_bytes=1024 * 1024,
            current_index=0,
        )
        self.assertEqual("allowed", status)
        self.assertIsNone(reason)

    def test_policy_checks_disallowed_active_content(self) -> None:
        for bad_file in ["macro.docm", "sheet.xlsm", "payload.exe", "script.vbs", "exploit.ps1"]:
            status, reason = attachment_policy.check_attachment_policy(
                filename=bad_file,
                mime_type="application/octet-stream",
                current_index=0,
            )
            self.assertEqual("rejected_security", status, f"Failed for {bad_file}")
            self.assertIn("active content", reason.lower())

    def test_policy_checks_oversized_file(self) -> None:
        status, reason = attachment_policy.check_attachment_policy(
            filename="huge_scan.pdf",
            mime_type="application/pdf",
            size_bytes=20 * 1024 * 1024,  # 20 MB > 15 MB
            current_index=0,
        )
        self.assertEqual("skipped_oversized", status)
        self.assertIn("exceeds", reason.lower())

    def test_policy_checks_attachment_count_limit(self) -> None:
        status, reason = attachment_policy.check_attachment_policy(
            filename="item6.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            current_index=5,  # 6th attachment (0-indexed)
        )
        self.assertEqual("skipped_count_limit", status)
        self.assertIn("count limit", reason.lower())

    def test_policy_cumulative_total_bytes_enforced(self) -> None:
        # File 1: 13 MB (<= 15 MB single limit, <= 25 MB cumulative) -> allowed
        status1, reason1 = attachment_policy.check_attachment_policy(
            filename="doc1.pdf",
            mime_type="application/pdf",
            size_bytes=13 * 1024 * 1024,
            current_index=0,
            cumulative_bytes=0,
        )
        self.assertEqual("allowed", status1)
        self.assertIsNone(reason1)

        # File 2: 13 MB (<= 15 MB single limit, BUT 13 MB + 13 MB = 26 MB > 25 MB total) -> skipped_total_oversized
        status2, reason2 = attachment_policy.check_attachment_policy(
            filename="doc2.pdf",
            mime_type="application/pdf",
            size_bytes=13 * 1024 * 1024,
            current_index=1,
            cumulative_bytes=13 * 1024 * 1024,
        )
        self.assertEqual("skipped_total_oversized", status2)
        self.assertIsNotNone(reason2)
        self.assertIn("exceeds maximum total limit", reason2)

    def test_policy_cumulative_total_bytes_boundary_limits(self) -> None:
        # Exact boundary: 15 MB + 10 MB = 25 MB (allowed)
        status_exact, reason_exact = attachment_policy.check_attachment_policy(
            filename="exact.pdf",
            mime_type="application/pdf",
            size_bytes=10 * 1024 * 1024,
            current_index=1,
            cumulative_bytes=15 * 1024 * 1024,
        )
        self.assertEqual("allowed", status_exact)
        self.assertIsNone(reason_exact)

        # 1 byte over boundary: 25 MB + 1 byte -> skipped_total_oversized
        status_over, reason_over = attachment_policy.check_attachment_policy(
            filename="extra.pdf",
            mime_type="application/pdf",
            size_bytes=1,
            current_index=2,
            cumulative_bytes=25 * 1024 * 1024,
        )
        self.assertEqual("skipped_total_oversized", status_over)
        self.assertIn("exceeds", reason_over)

    # ==========================================================================
    # 2. Adversarial / Strict Fail-Closed: Unknown Size & MIME Never Allowed
    # ==========================================================================

    def test_policy_unknown_or_invalid_size_never_allowed(self) -> None:
        invalid_sizes = [None, 0, -1, -500, "1024"]
        for bad_size in invalid_sizes:
            status, reason = attachment_policy.check_attachment_policy(
                filename="document.pdf",
                mime_type="application/pdf",
                size_bytes=bad_size,  # type: ignore[arg-type]
                current_index=0,
            )
            self.assertEqual(
                "rejected_unspecified_size",
                status,
                f"Size {bad_size!r} must result in rejected_unspecified_size, got {status}",
            )
            self.assertIsNotNone(reason)
            self.assertNotEqual("allowed", status)

    def test_policy_unknown_or_unsupported_mime_never_allowed(self) -> None:
        unsupported_mimes = [
            None,
            "",
            "   ",
            "application/octet-stream",
            "application/x-unknown",
            "application/zip",
            "text/html",
        ]
        for bad_mime in unsupported_mimes:
            status, reason = attachment_policy.check_attachment_policy(
                filename="document.pdf",
                mime_type=bad_mime,  # type: ignore[arg-type]
                size_bytes=2048,
                current_index=0,
            )
            self.assertEqual(
                "rejected_unsupported_type",
                status,
                f"MIME {bad_mime!r} must result in rejected_unsupported_type, got {status}",
            )
            self.assertIsNotNone(reason)
            self.assertNotEqual("allowed", status)

    # ==========================================================================
    # 3. Hermetic Structured MIME Inspection & Adversarial Body Injection
    # ==========================================================================

    def test_inspect_mime_tree_plain_text_message_has_no_attachments(self) -> None:
        raw_eml = attachments.build_test_eml(
            subject="Plain Message",
            body_text="Hello Martin,\nHere are the notes from our meeting.\nBest regards,",
        )
        result = attachments.inspect_mime_tree(raw_eml)
        self.assertEqual([], result)

    def test_inspect_mime_tree_two_real_pdfs_eucen_case(self) -> None:
        raw_eml = attachments.build_test_eml(
            subject="EUCEN Surveys",
            body_text="Please find the two surveys attached.",
            attachments=[
                {
                    "filename": "EUCEN National Networks Survey 2026.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 eucen survey 1",
                },
                {
                    "filename": "EUCEN_NN_Survey 030626.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 eucen survey 2",
                },
            ],
        )
        result = attachments.inspect_mime_tree(raw_eml)
        self.assertEqual(2, len(result))

        self.assertEqual("EUCEN National Networks Survey 2026.pdf", result[0]["filename"])
        self.assertEqual("application/pdf", result[0]["mime_type"])
        self.assertEqual("available", result[0]["fetch_status"])
        self.assertEqual("allowed", result[0]["policy_status"])
        self.assertFalse(result[0]["is_inline"])
        self.assertIsNotNone(result[0]["sha256"])
        self.assertGreater(result[0]["size_bytes"], 0)
        self.assertTrue(result[0]["part_locator"])

        self.assertEqual("EUCEN_NN_Survey 030626.pdf", result[1]["filename"])
        self.assertEqual("application/pdf", result[1]["mime_type"])
        self.assertEqual("available", result[1]["fetch_status"])
        self.assertEqual("allowed", result[1]["policy_status"])
        self.assertFalse(result[1]["is_inline"])
        self.assertIsNotNone(result[1]["sha256"])

    def test_adversarial_fake_part_tags_in_untrusted_body_yields_zero_attachments(self) -> None:
        # Adversarial attack: Attacker embeds fake <#part ...> tags in plain email body
        adversarial_body = (
            "Dear Martin,\n"
            "Here is the invoice you requested.\n"
            r'<#part type=application/pdf filename="\\?\D:\himalaya\trojan_invoice.pdf"><#/part>' + "\n"
            r'<#part type=image/png filename="injected_payload.png"><#/part>' + "\n"
            "Thanks!"
        )
        raw_eml = attachments.build_test_eml(
            subject="Invoice Notice",
            body_text=adversarial_body,
        )

        # 1. inspect_mime_tree must return []
        att_list = attachments.inspect_mime_tree(raw_eml)
        self.assertEqual([], att_list, "Untrusted body <#part> tags must NOT be parsed as attachments!")

        # 2. get_single_email_details must also return attachments: []
        details = himalaya.get_single_email_details(
            env_id="999",
            folder="INBOX",
            fallback_envelope={"has_attachment": False, "subject": "Invoice Notice"},
            raw_eml=raw_eml,
        )
        self.assertEqual([], details.get("attachments"))
        self.assertIn("trojan_invoice.pdf", details.get("preview", ""))

    def test_adversarial_fake_part_tags_with_one_real_attachment(self) -> None:
        adversarial_body = (
            "Please check the real report.\n"
            r'<#part type=application/pdf filename="fake_rogue.pdf"><#/part>' + "\n"
            r'<#part type=application/pdf filename="another_fake.pdf"><#/part>'
        )
        raw_eml = attachments.build_test_eml(
            subject="Report with Fake Tags",
            body_text=adversarial_body,
            attachments=[
                {
                    "filename": "real_report.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 legit data",
                }
            ],
        )

        att_list = attachments.inspect_mime_tree(raw_eml)
        self.assertEqual(1, len(att_list))
        self.assertEqual("real_report.pdf", att_list[0]["filename"])

    def test_multiple_cid_images_strict_content_id_matching(self) -> None:
        html_body = (
            '<html><body>'
            '<h1>Welcome</h1>'
            '<img src="cid:banner_cid">'
            '<p>Best regards,</p>'
            '<img src="cid:sig_cid">'
            '</body></html>'
        )
        raw_eml = attachments.build_test_eml(
            subject="Newsletter with Logos",
            body_text="Welcome! Best regards,",
            body_html=html_body,
            inline_images=[
                {
                    "cid": "banner_cid",
                    "filename": "banner.png",
                    "mime_type": "image/png",
                    "data": b"\x89PNG banner",
                },
                {
                    "cid": "sig_cid",
                    "filename": "signature.png",
                    "mime_type": "image/png",
                    "data": b"\x89PNG sig",
                },
                {
                    "cid": "photo_cid",
                    "filename": "photo.png",
                    "mime_type": "image/png",
                    "data": b"\x89PNG unreferenced",
                },
            ],
            attachments=[
                {
                    "filename": "image001.png",
                    "mime_type": "image/png",
                    "data": b"\x89PNG image001",
                }
            ],
        )

        att_list = attachments.inspect_mime_tree(raw_eml)
        self.assertEqual(4, len(att_list))

        by_filename = {att["filename"]: att for att in att_list}

        # 1. banner.png is referenced -> is_inline: True
        self.assertTrue(by_filename["banner.png"]["is_inline"])
        self.assertEqual("banner_cid", by_filename["banner.png"]["content_id"])

        # 2. signature.png is referenced -> is_inline: True
        self.assertTrue(by_filename["signature.png"]["is_inline"])
        self.assertEqual("sig_cid", by_filename["signature.png"]["content_id"])

        # 3. photo.png is NOT referenced in body -> is_inline: False
        self.assertFalse(by_filename["photo.png"]["is_inline"])
        self.assertEqual("photo_cid", by_filename["photo.png"]["content_id"])

        # 4. image001.png has no CID -> is_inline: False (no filename guessing!)
        self.assertFalse(by_filename["image001.png"]["is_inline"])
        self.assertIsNone(by_filename["image001.png"]["content_id"])

    # ==========================================================================
    # 4. Attachment Candidate Binding & Drift Detection
    # ==========================================================================

    def test_bind_attachment_candidate_valid_and_invalid(self) -> None:
        att = {
            "filename": "survey.pdf",
            "mime_type": "application/pdf",
            "part_locator": "2",
            "size_bytes": 1024,
            "sha256": "a" * 64,
            "policy_status": "allowed",
            "provenance": attachments.PROVENANCE_RFC822,
        }
        bound = attachments.bind_attachment_candidate(
            attachment=att,
            account="BOKU-MARTIN",
            folder="INBOX",
            envelope_id="9560",
            message_id="<msg123@boku.ac.at>",
        )
        self.assertEqual("BOKU-MARTIN", bound["account"])
        self.assertEqual("INBOX", bound["folder"])
        self.assertEqual("9560", bound["envelope_id"])
        self.assertEqual("msg123@boku.ac.at", bound["message_id"])
        self.assertEqual("2", bound["part_locator"])

        with self.assertRaises(ValueError):
            attachments.bind_attachment_candidate(
                attachment=att,
                account="",
                folder="INBOX",
                envelope_id="9560",
                message_id="msg123@boku.ac.at",
            )
        with self.assertRaises(ValueError):
            attachments.bind_attachment_candidate(
                attachment=att,
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="9560",
                message_id="",
            )

    def test_drift_account_mismatch_raises_account_drift_error(self) -> None:
        cand = {
            "filename": "doc.pdf",
            "account": "ACCOUNT-A",
            "message_id": "msg-001@example.com",
            "folder": "INBOX",
            "envelope_id": "42",
            "part_locator": "1",
        }
        with self.assertRaises(attachments.AccountDriftError) as ctx:
            attachments.verify_attachment_drift(cand, expected_account="ACCOUNT-B")
        self.assertIn("Account drift detected", str(ctx.exception))

    def test_drift_message_id_mismatch_raises_message_id_drift_error(self) -> None:
        cand = {
            "filename": "doc.pdf",
            "account": "ACCOUNT-A",
            "message_id": "msg-001@example.com",
            "folder": "INBOX",
            "envelope_id": "42",
            "part_locator": "1",
        }
        res = attachments.verify_attachment_drift(cand, expected_message_id="<msg-001@example.com>")
        self.assertEqual("verified", res["status"])

        with self.assertRaises(attachments.MessageIdDriftError) as ctx:
            attachments.verify_attachment_drift(cand, expected_message_id="<different-msg@example.com>")
        self.assertIn("Message-ID drift detected", str(ctx.exception))

    def test_drift_location_folder_or_envelope_raises_location_drift_error(self) -> None:
        cand = {
            "filename": "doc.pdf",
            "account": "ACCOUNT-A",
            "message_id": "msg-001@example.com",
            "folder": "INBOX",
            "envelope_id": "42",
            "part_locator": "1",
        }
        with self.assertRaises(attachments.LocationDriftError) as ctx:
            attachments.verify_attachment_drift(cand, expected_folder="Projekte/USAGE-NG")
        self.assertIn("Folder drift detected", str(ctx.exception))

        with self.assertRaises(attachments.LocationDriftError) as ctx:
            attachments.verify_attachment_drift(cand, expected_envelope_id="43")
        self.assertIn("Envelope-ID drift detected", str(ctx.exception))

    def test_drift_part_locator_mismatch_raises_part_locator_drift_error(self) -> None:
        cand = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "account": "ACCOUNT-A",
            "message_id": "msg-001@example.com",
            "folder": "INBOX",
            "envelope_id": "42",
            "part_locator": "2",
            "sha256": "hash_initial_abc",
        }

        current_parts_missing = [
            {"part_locator": "1", "filename": "other.pdf", "mime_type": "application/pdf", "sha256": "h1"}
        ]
        with self.assertRaises(attachments.PartLocatorDriftError) as ctx:
            attachments.verify_attachment_drift(cand, current_mime_parts=current_parts_missing)
        self.assertIn("part locator '2' not found", str(ctx.exception))

        current_parts_renamed = [
            {"part_locator": "2", "filename": "renamed_doc.pdf", "mime_type": "application/pdf", "sha256": "hash_initial_abc"}
        ]
        with self.assertRaises(attachments.PartLocatorDriftError) as ctx:
            attachments.verify_attachment_drift(cand, current_mime_parts=current_parts_renamed)
        self.assertIn("filename changed", str(ctx.exception))

        current_parts_mime_changed = [
            {"part_locator": "2", "filename": "doc.pdf", "mime_type": "application/zip", "sha256": "hash_initial_abc"}
        ]
        with self.assertRaises(attachments.PartLocatorDriftError) as ctx:
            attachments.verify_attachment_drift(cand, current_mime_parts=current_parts_mime_changed)
        self.assertIn("MIME type changed", str(ctx.exception))

    def test_drift_hash_mismatch_raises_hash_drift_error(self) -> None:
        cand = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "account": "ACCOUNT-A",
            "message_id": "msg-001@example.com",
            "folder": "INBOX",
            "envelope_id": "42",
            "part_locator": "2",
            "sha256": "original_sha256_hash",
        }

        current_parts_tampered = [
            {"part_locator": "2", "filename": "doc.pdf", "mime_type": "application/pdf", "sha256": "tampered_sha256_hash"}
        ]
        with self.assertRaises(attachments.HashDriftError) as ctx:
            attachments.verify_attachment_drift(cand, current_mime_parts=current_parts_tampered)
        self.assertIn("Hash drift detected", str(ctx.exception))

        with self.assertRaises(attachments.HashDriftError) as ctx:
            attachments.verify_attachment_drift(cand, verify_hash="different_sha256_hash")
        self.assertIn("Hash drift detected", str(ctx.exception))

    def test_drift_all_checks_pass(self) -> None:
        cand = {
            "filename": "doc.pdf",
            "mime_type": "application/pdf",
            "account": "ACCOUNT-A",
            "message_id": "msg-001@example.com",
            "folder": "INBOX",
            "envelope_id": "42",
            "part_locator": "2",
            "sha256": "matching_sha256_hash",
        }
        current_parts = [
            {"part_locator": "2", "filename": "doc.pdf", "mime_type": "application/pdf", "sha256": "matching_sha256_hash"}
        ]
        res = attachments.verify_attachment_drift(
            cand,
            expected_account="ACCOUNT-A",
            expected_folder="INBOX",
            expected_envelope_id="42",
            expected_message_id="<msg-001@example.com>",
            current_mime_parts=current_parts,
            verify_hash="matching_sha256_hash",
        )
        self.assertEqual("verified", res["status"])

    # ==========================================================================
    # 5. Mail Desk Himalaya Client & Manifest Contract
    # ==========================================================================

    def test_op_inspect_attachments_hermetic_success(self) -> None:
        raw_eml = attachments.build_test_eml(
            subject="Inspection Test",
            message_id="<test-inspect-001@boku.ac.at>",
            attachments=[
                {
                    "filename": "agenda.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 agenda data",
                }
            ],
        )
        res = client.op_inspect_attachments(
            envelope_id="7195",
            folder="INBOX",
            account="BOKU-MARTIN",
            expected_message_id="test-inspect-001@boku.ac.at",
            raw_eml=raw_eml,
        )
        self.assertEqual("7195", res["envelope_id"])
        self.assertEqual("INBOX", res["folder"])
        self.assertEqual("BOKU-MARTIN", res["account"])
        self.assertEqual("test-inspect-001@boku.ac.at", res["message_id"])
        self.assertEqual("Inspection Test", res["subject"])
        self.assertEqual(1, res["attachment_count"])
        self.assertEqual("agenda.pdf", res["attachments"][0]["filename"])
        self.assertEqual("allowed", res["attachments"][0]["policy_status"])

        self.assertEqual(1, len(res["bound_candidates"]))
        self.assertEqual("BOKU-MARTIN", res["bound_candidates"][0]["account"])
        self.assertEqual("7195", res["bound_candidates"][0]["envelope_id"])

    def test_op_inspect_attachments_expected_message_id_drift(self) -> None:
        raw_eml = attachments.build_test_eml(
            subject="Inspection Test",
            message_id="<actual-mid@boku.ac.at>",
            attachments=[{"filename": "doc.pdf", "mime_type": "application/pdf", "data": b"%PDF-1.4"}],
        )
        with self.assertRaises(attachments.MessageIdDriftError):
            client.op_inspect_attachments(
                envelope_id="7195",
                folder="INBOX",
                account="BOKU-MARTIN",
                expected_message_id="different-expected-mid@boku.ac.at",
                raw_eml=raw_eml,
            )

    def test_execute_manifest_inspect_attachments(self) -> None:
        raw_eml = attachments.build_test_eml(
            subject="Batch Manifest Inspection",
            message_id="<manifest-att@boku.ac.at>",
            attachments=[
                {
                    "filename": "minutes.pdf",
                    "mime_type": "application/pdf",
                    "data": b"%PDF-1.4 minutes",
                }
            ],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_file = Path(tmp_dir) / "inspect-manifest.json"
            manifest_file.write_text(
                json.dumps({
                    "account": "BOKU-MARTIN",
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "inspect_attachments",
                            "envelope_id": "888",
                            "folder": "INBOX",
                            "expected_message_id": "manifest-att@boku.ac.at",
                        }
                    ],
                }),
                encoding="utf-8",
            )
            with patch.object(client, "fetch_raw_message_eml", return_value=raw_eml):
                res = client.execute_manifest(manifest_file)

            self.assertTrue(res["all_succeeded"])
            self.assertEqual(1, len(res["results"]))
            item_res = res["results"][0]["result"]
            self.assertEqual("888", item_res["envelope_id"])
            self.assertEqual(1, item_res["attachment_count"])
            self.assertEqual("minutes.pdf", item_res["attachments"][0]["filename"])

    def test_draft_manifest_includes_structured_attachments(self) -> None:
        email_item = {
            "envelope_id": "42",
            "folder": "INBOX",
            "message_id": "eucen-survey@eucen.eu",
            "subject": "EUCEN Survey 2026",
            "from": "EUCEN <survey@eucen.eu>",
            "to": "BOKU <office@boku.ac.at>",
            "date": "2026-07-09",
            "preview": "Survey details.",
            "attachments": [
                {
                    "filename": "EUCEN National Networks Survey 2026.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 1024 * 1024,
                    "sha256": "a" * 64,
                    "part_locator": "1",
                    "fetch_status": "available",
                    "policy_status": "allowed",
                    "is_inline": False,
                    "provenance": attachments.PROVENANCE_RFC822,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            manifest = classifier.draft_manifest(
                [email_item],
                workspace_root=ws,
                sync_sent=False,
                account="BOKU-MARTIN",
            )
            self.assertEqual(1, len(manifest["items"]))
            item = manifest["items"][0]
            self.assertIn("attachments", item)
            self.assertEqual(1, len(item["attachments"]))
            self.assertEqual("EUCEN National Networks Survey 2026.pdf", item["attachments"][0]["filename"])
            self.assertEqual("allowed", item["attachments"][0]["policy_status"])
            self.assertEqual("BOKU-MARTIN", item["attachments"][0]["account"])

    def test_run_draft_mode_end_to_end_preserves_attachments_in_batch_manifest(self) -> None:
        email_item = {
            "envelope_id": "42",
            "folder": "INBOX",
            "message_id": "eucen-survey@eucen.eu",
            "subject": "EUCEN Survey 2026",
            "from": "EUCEN <survey@eucen.eu>",
            "to": "BOKU <office@boku.ac.at>",
            "date": "2026-07-09",
            "preview": "Survey details.",
            "attachments": [
                {
                    "filename": "EUCEN National Networks Survey 2026.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 1024 * 1024,
                    "sha256": "a" * 64,
                    "part_locator": "1",
                    "fetch_status": "available",
                    "policy_status": "allowed",
                    "is_inline": False,
                    "provenance": attachments.PROVENANCE_RFC822,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_file = data_dir / "batch-manifest.json"

            config = {
                "folder": "INBOX",
                "count": 1,
                "expected_count": 1,
                "allow_fewer": False,
                "output_file": str(output_file),
            }
            dependencies = {
                "get_unprocessed_emails": Mock(return_value=([email_item], 0)),
                "load_sent_index": Mock(return_value={"sent": []}),
                "get_single_email_details": Mock(return_value=email_item),
            }

            with patch.object(classifier, "sync_sent_items"):
                res = draft_mode.run_draft_mode(
                    config=config,
                    account="BOKU-MARTIN",
                    data_dir=data_dir,
                    dependencies=dependencies,
                )
            self.assertTrue(res["ok"])
            self.assertTrue(output_file.exists())

            written_manifest = json.loads(output_file.read_text(encoding="utf-8"))
            self.assertEqual(1, len(written_manifest["items"]))
            item = written_manifest["items"][0]
            self.assertIn("attachments", item)
            self.assertEqual(1, len(item["attachments"]))
            self.assertEqual("EUCEN National Networks Survey 2026.pdf", item["attachments"][0]["filename"])
            self.assertEqual("allowed", item["attachments"][0]["policy_status"])
            self.assertEqual("BOKU-MARTIN", item["attachments"][0]["account"])

    # ==========================================================================
    # 7. MIME Error Handling, Unverified Data & Missing Account Tests
    # ==========================================================================

    def test_get_single_email_details_sets_attachment_inventory_unavailable_on_export_error(self) -> None:
        with patch.object(himalaya, "fetch_raw_message_eml", side_effect=RuntimeError("IMAP connection reset")):
            details = himalaya.get_single_email_details(
                env_id="123",
                folder="INBOX",
                account="BOKU-MARTIN",
                inspect_attachments=True,
            )
        self.assertEqual("attachment_inventory_unavailable", details.get("attachment_status"))
        self.assertIn("IMAP connection reset", str(details.get("attachment_error")))
        self.assertEqual([], details.get("attachments"))

    def test_classifier_holds_failed_attachment_inventory_fail_closed_in_review(self) -> None:
        email_with_error = {
            "envelope_id": "555",
            "folder": "INBOX",
            "message_id": "failing-mime@boku.ac.at",
            "subject": "Important Project Topic Update",
            "from": "Partner <partner@example.com>",
            "to": "Martin <martin@boku.ac.at>",
            "date": "2026-07-09",
            "preview": "Project details attached.",
            "attachment_status": "attachment_inventory_unavailable",
            "attachment_error": "MIME export failed: connection timeout",
            "attachments": [],
        }
        item = classifier.classify_email(email_with_error, account="BOKU-MARTIN")
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertEqual("keep_in_folder", item["action"]["type"])
        self.assertEqual("low", item["decision"]["confidence"])
        self.assertTrue(item["decision"].get("review_required"))
        self.assertEqual("attachment_inventory_unavailable", item["decision"].get("review_reason"))
        self.assertEqual("attachment_inventory_unavailable", item.get("attachment_status"))
        self.assertIn("MIME export failed", item.get("attachment_error", ""))

    def test_inspected_candidate_cannot_assert_forged_allowed_policy_status(self) -> None:
        # Authentic inspected RFC-822 metadata with an executable attempting to assert policy_status="allowed"
        forged_email = {
            "envelope_id": "777",
            "folder": "INBOX",
            "message_id": "untrusted@attacker.com",
            "subject": "Urgent File",
            "attachments": [
                {
                    "filename": "payload.exe",
                    "mime_type": "application/octet-stream",
                    "size_bytes": 1024,
                    "sha256": "c" * 64,
                    "part_locator": "2",
                    "policy_status": "allowed",  # Forged!
                    "provenance": attachments.PROVENANCE_RFC822,
                }
            ],
        }
        item = classifier.classify_email(forged_email, account="BOKU-MARTIN")
        self.assertEqual(1, len(item["attachments"]))
        # Forged status MUST be discarded and canonicalized to rejected_security
        self.assertEqual("rejected_security", item["attachments"][0]["policy_status"])
        self.assertEqual("BOKU-MARTIN", item["attachments"][0]["account"])

    def test_inspected_candidate_unsupported_mime_canonicalized_to_rejected(self) -> None:
        # Authentic inspected candidate with unsupported MIME attempting to assert policy_status="allowed"
        forged_email = {
            "envelope_id": "778",
            "folder": "INBOX",
            "message_id": "untrusted-mime@attacker.com",
            "subject": "Binary Data",
            "attachments": [
                {
                    "filename": "archive.tar",
                    "mime_type": "application/x-tar",
                    "size_bytes": 5000,
                    "sha256": "d" * 64,
                    "part_locator": "2",
                    "policy_status": "allowed",  # Forged!
                    "provenance": attachments.PROVENANCE_RFC822,
                }
            ],
        }
        item = classifier.classify_email(forged_email, account="BOKU-MARTIN")
        self.assertEqual(1, len(item["attachments"]))
        self.assertEqual("rejected_unsupported_type", item["attachments"][0]["policy_status"])

    def test_adversarial_unverified_candidate_missing_provenance_rejected(self) -> None:
        # Unverified candidate without RFC-822 provenance must be rejected fail-closed
        raw_att = {
            "filename": "document.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024,
            "sha256": "e" * 64,
            "part_locator": "1",
            # missing provenance
        }
        with self.assertRaises(attachments.AttachmentInventoryValidationError) as ctx:
            attachments.canonicalize_and_bind_attachments(
                raw_attachments=[raw_att],
                account="BOKU-MARTIN",
                folder="INBOX",
                envelope_id="123",
                message_id="msg@example.com",
            )
        self.assertIn("provenance", str(ctx.exception).lower())

        # When processed via classify_email, it must hold item in review with attachment_inventory_unavailable
        item = classifier.classify_email(
            {
                "envelope_id": "123",
                "folder": "INBOX",
                "message_id": "msg@example.com",
                "attachments": [raw_att],
            },
            account="BOKU-MARTIN",
        )
        self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertEqual("low", item["decision"]["confidence"])
        self.assertTrue(item["decision"]["review_required"])
        self.assertEqual("attachment_inventory_unavailable", item["decision"]["review_reason"])
        self.assertEqual([], item["attachments"])

    def test_adversarial_fake_size_rejected(self) -> None:
        # Fake / non-positive / non-integer sizes must be rejected
        for bad_size in [-1, 0, None, "1024", True]:
            raw_att = {
                "filename": "document.pdf",
                "mime_type": "application/pdf",
                "size_bytes": bad_size,
                "sha256": "f" * 64,
                "part_locator": "1",
                "provenance": attachments.PROVENANCE_RFC822,
            }
            with self.assertRaises(attachments.AttachmentInventoryValidationError) as ctx:
                attachments.canonicalize_and_bind_attachments(
                    raw_attachments=[raw_att],
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="123",
                    message_id="msg@example.com",
                )
            self.assertIn("size_bytes", str(ctx.exception).lower())

            item = classifier.classify_email(
                {"envelope_id": "123", "folder": "INBOX", "message_id": "<msg@example.com>", "attachments": [raw_att]},
                account="BOKU-MARTIN",
            )
            self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
            self.assertEqual([], item["attachments"])

    def test_adversarial_fake_mime_type_rejected(self) -> None:
        # Malformed or missing MIME types must be rejected
        for bad_mime in ["", "   ", "invalid_no_slash", None, 1234]:
            raw_att = {
                "filename": "document.pdf",
                "mime_type": bad_mime,
                "size_bytes": 2048,
                "sha256": "0" * 64,
                "part_locator": "1",
                "provenance": attachments.PROVENANCE_RFC822,
            }
            with self.assertRaises(attachments.AttachmentInventoryValidationError) as ctx:
                attachments.canonicalize_and_bind_attachments(
                    raw_attachments=[raw_att],
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="123",
                    message_id="msg@example.com",
                )
            self.assertIn("mime_type", str(ctx.exception).lower())

            item = classifier.classify_email(
                {"envelope_id": "123", "folder": "INBOX", "message_id": "<msg@example.com>", "attachments": [raw_att]},
                account="BOKU-MARTIN",
            )
            self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
            self.assertEqual([], item["attachments"])

    def test_adversarial_fake_sha256_hash_rejected(self) -> None:
        # Fake / non-64-hex SHA-256 hashes must be rejected
        for bad_hash in ["", "not_64_chars", "g" * 64, None, 12345]:
            raw_att = {
                "filename": "document.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 2048,
                "sha256": bad_hash,
                "part_locator": "1",
                "provenance": attachments.PROVENANCE_RFC822,
            }
            with self.assertRaises(attachments.AttachmentInventoryValidationError) as ctx:
                attachments.canonicalize_and_bind_attachments(
                    raw_attachments=[raw_att],
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="123",
                    message_id="msg@example.com",
                )
            self.assertIn("sha256", str(ctx.exception).lower())

            item = classifier.classify_email(
                {"envelope_id": "123", "folder": "INBOX", "message_id": "<msg@example.com>", "attachments": [raw_att]},
                account="BOKU-MARTIN",
            )
            self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
            self.assertEqual([], item["attachments"])

    def test_adversarial_fake_or_invented_locator_rejected(self) -> None:
        # Invented locators ('part_1') or traversal patterns must be rejected
        for bad_loc in ["part_1", "../1", "", "   ", None, "abc", "1..2"]:
            raw_att = {
                "filename": "document.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 2048,
                "sha256": "1" * 64,
                "part_locator": bad_loc,
                "provenance": attachments.PROVENANCE_RFC822,
            }
            with self.assertRaises(attachments.AttachmentInventoryValidationError) as ctx:
                attachments.canonicalize_and_bind_attachments(
                    raw_attachments=[raw_att],
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="123",
                    message_id="msg@example.com",
                )
            self.assertIn("part_locator", str(ctx.exception).lower())

            item = classifier.classify_email(
                {"envelope_id": "123", "folder": "INBOX", "message_id": "<msg@example.com>", "attachments": [raw_att]},
                account="BOKU-MARTIN",
            )
            self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
            self.assertEqual([], item["attachments"])

    def test_adversarial_forged_fetch_status_rejected(self) -> None:
        # Callers attempting to forge fetch_status ('downloaded', 'verified', 'bypassed') must be rejected
        for bad_status in ["downloaded", "verified", "bypassed", "quarantined", "allowed"]:
            raw_att = {
                "filename": "document.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 2048,
                "sha256": "2" * 64,
                "part_locator": "1",
                "provenance": attachments.PROVENANCE_RFC822,
                "fetch_status": bad_status,
            }
            with self.assertRaises(attachments.AttachmentInventoryValidationError) as ctx:
                attachments.canonicalize_and_bind_attachments(
                    raw_attachments=[raw_att],
                    account="BOKU-MARTIN",
                    folder="INBOX",
                    envelope_id="123",
                    message_id="msg@example.com",
                )
            self.assertIn("fetch_status", str(ctx.exception).lower())

            item = classifier.classify_email(
                {"envelope_id": "123", "folder": "INBOX", "message_id": "<msg@example.com>", "attachments": [raw_att]},
                account="BOKU-MARTIN",
            )
            self.assertEqual("attachment_inventory_unavailable", item["attachment_status"])
            self.assertEqual([], item["attachments"])

    def test_fallback_envelope_attachments_unverified_marked_unavailable(self) -> None:
        # get_single_email_details with unverified fallback_envelope attachments and inspect_attachments=False
        details = himalaya.get_single_email_details(
            env_id="888",
            folder="INBOX",
            fallback_envelope={
                "has_attachment": True,
                "attachments": [{"filename": "unverified.pdf", "policy_status": "allowed"}],
            },
            inspect_attachments=False,
        )
        self.assertEqual("attachment_inventory_unavailable", details.get("attachment_status"))

    def test_missing_account_stops_draft_manifest_fail_closed(self) -> None:
        email_with_attachments = {
            "envelope_id": "999",
            "folder": "INBOX",
            "message_id": "need-account@example.com",
            "subject": "Attachments Present",
            "attachments": [
                {
                    "filename": "doc.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 1024,
                    "sha256": "3" * 64,
                    "part_locator": "1",
                    "provenance": attachments.PROVENANCE_RFC822,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            with self.assertRaises(ValueError) as ctx:
                classifier.draft_manifest([email_with_attachments], workspace_root=ws, sync_sent=False, account=None)
            self.assertIn("Missing account", str(ctx.exception))

    def test_canonicalize_and_bind_attachments_fails_without_account(self) -> None:
        raw_atts = [
            {
                "filename": "doc.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 1024,
                "sha256": "4" * 64,
                "part_locator": "1",
                "provenance": attachments.PROVENANCE_RFC822,
            }
        ]
        with self.assertRaises(ValueError) as ctx:
            attachments.canonicalize_and_bind_attachments(
                raw_attachments=raw_atts,
                account="",
                folder="INBOX",
                envelope_id="1",
                message_id="mid@example.com",
            )
        self.assertIn("Missing account", str(ctx.exception))

    def test_op_inspect_attachments_aborts_fail_closed_without_account_before_export(self) -> None:
        # Direct regression test: op_inspect_attachments must abort fail-closed before any export
        with patch.object(client, "fetch_raw_message_eml") as mock_fetch:
            with self.assertRaises(ValueError) as ctx:
                client.op_inspect_attachments(envelope_id="7195", folder="INBOX", account=None)
            self.assertIn("account is required", str(ctx.exception).lower())
            mock_fetch.assert_not_called()

        with patch.object(client, "fetch_raw_message_eml") as mock_fetch:
            with self.assertRaises(ValueError) as ctx:
                client.op_inspect_attachments(envelope_id="7195", folder="INBOX", account="")
            self.assertIn("account is required", str(ctx.exception).lower())
            mock_fetch.assert_not_called()

    def test_direct_cli_rejects_inspect_attachments_subcommand(self) -> None:
        # Direct CLI inspect-attachments is removed to ensure production execution is strictly manifest-driven
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                client.parse_args(["inspect-attachments", "7195"])

    def test_execute_manifest_fails_closed_without_account_for_inspect_attachments(self) -> None:
        # Manifest without account for inspect_attachments fails closed
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_file = Path(tmp_dir) / "inspect-no-acc.json"
            manifest_file.write_text(
                json.dumps({
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "inspect_attachments",
                            "envelope_id": "888",
                            "folder": "INBOX",
                        }
                    ],
                }),
                encoding="utf-8",
            )
            with patch.object(client, "fetch_raw_message_eml") as mock_fetch:
                res = client.execute_manifest(manifest_file, account=None)
                self.assertFalse(res["all_succeeded"])
                self.assertIn("account is required", str(res["results"][0].get("error", "")).lower())
                mock_fetch.assert_not_called()

    def test_execute_manifest_inspect_attachments_account_drift_fails(self) -> None:
        # Operation account cannot override bound manifest account; must fail closed with AccountDriftError
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_file = Path(tmp_dir) / "inspect-drift-acc.json"
            manifest_file.write_text(
                json.dumps({
                    "account": "BOKU-MARTIN",
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "inspect_attachments",
                            "envelope_id": "888",
                            "folder": "INBOX",
                            "account": "EVIL-ACCOUNT",
                            "expected_message_id": "mid@boku.ac.at",
                        }
                    ],
                }),
                encoding="utf-8",
            )
            with patch.object(client, "fetch_raw_message_eml") as mock_fetch:
                res = client.execute_manifest(manifest_file)
                self.assertFalse(res["all_succeeded"])
                self.assertFalse(res["results"][0]["success"])
                err = str(res["results"][0].get("error", ""))
                self.assertIn("Account drift detected", err)
                mock_fetch.assert_not_called()

    def test_execute_manifest_inspect_attachments_matching_account_succeeds(self) -> None:
        # Operation account matching bound manifest account serves as verified evidence and succeeds
        raw_eml = attachments.build_test_eml(
            subject="Matching Account Test",
            message_id="<match-mid@boku.ac.at>",
            attachments=[{"filename": "doc.pdf", "mime_type": "application/pdf", "data": b"%PDF-1.4"}],
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_file = Path(tmp_dir) / "inspect-match-acc.json"
            manifest_file.write_text(
                json.dumps({
                    "account": "BOKU-MARTIN",
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "inspect_attachments",
                            "envelope_id": "999",
                            "folder": "INBOX",
                            "account": "BOKU-MARTIN",
                            "expected_message_id": "match-mid@boku.ac.at",
                        }
                    ],
                }),
                encoding="utf-8",
            )
            with patch.object(client, "fetch_raw_message_eml", return_value=raw_eml) as mock_fetch:
                res = client.execute_manifest(manifest_file)
                self.assertTrue(res["all_succeeded"])
                self.assertTrue(res["results"][0]["success"])
                mock_fetch.assert_called_once_with("999", folder="INBOX", account="BOKU-MARTIN")

    def test_op_inspect_attachments_fails_closed_when_message_id_missing(self) -> None:
        # op_inspect_attachments must fail closed if Message-ID is missing when attachments exist
        raw_eml = attachments.build_test_eml(
            subject="Missing MID Test",
            message_id=None,
            attachments=[{"filename": "unbound.pdf", "mime_type": "application/pdf", "data": b"%PDF-1.4 data"}],
        )
        with self.assertRaises(ValueError) as ctx:
            client.op_inspect_attachments(
                envelope_id="7195",
                folder="INBOX",
                account="BOKU-MARTIN",
                raw_eml=raw_eml,
            )
        self.assertIn("Missing Message-ID", str(ctx.exception))

        # Also verify via manifest execution: operation fails closed with success: False
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_file = Path(tmp_dir) / "inspect-missing-mid.json"
            manifest_file.write_text(
                json.dumps({
                    "account": "BOKU-MARTIN",
                    "delete_input_on_success": False,
                    "operations": [
                        {
                            "action": "inspect_attachments",
                            "envelope_id": "7195",
                            "folder": "INBOX",
                        }
                    ],
                }),
                encoding="utf-8",
            )
            with patch.object(client, "fetch_raw_message_eml", return_value=raw_eml):
                res = client.execute_manifest(manifest_file)
                self.assertFalse(res["all_succeeded"])
                self.assertFalse(res["results"][0]["success"])
                self.assertIn("missing message-id", str(res["results"][0].get("error", "")).lower())


if __name__ == "__main__":
    unittest.main()
