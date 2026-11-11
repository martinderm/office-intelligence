"""Offline regression tests for fail-closed MIME inventory validation."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from core import attachments  # noqa: E402
from core import attachment_reclassification as reclassification  # noqa: E402


class MimeStructureHardeningTests(unittest.TestCase):
    def test_valid_message_preserves_attachment_inventory(self):
        raw = attachments.build_test_eml(attachments=[{
            "filename": "report.pdf", "mime_type": "application/pdf", "data": b"pdf-data",
        }])
        result = attachments.inspect_mime_tree(raw)
        self.assertEqual([item["filename"] for item in result], ["report.pdf"])

    def test_duplicate_singletons_fail_closed_at_root_and_nested_part(self):
        for header, values in (
            (b"Content-Type", (b"text/plain", b"application/pdf")),
            (b"Content-Transfer-Encoding", (b"base64", b"7bit")),
            (b"Content-Disposition", (b"attachment", b"inline")),
        ):
            for nested in (False, True):
                with self.subTest(header=header, nested=nested):
                    fields = header + b": " + values[0] + b"\r\n" + header + b": " + values[1] + b"\r\n"
                    raw = (b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n" + fields
                           + b"\r\nAAAA\r\n--x--\r\n") if nested else fields + b"\r\nAAAA\r\n"
                    with self.assertRaises(attachments.MimeStructureError):
                        attachments.inspect_mime_tree(raw)

    def test_nested_missing_boundary_and_invalid_base64_fail_closed(self):
        cases = [
            b"Content-Type: multipart/mixed; boundary=o\r\n\r\n--o\r\n"
            b"Content-Type: multipart/mixed; boundary=i\r\n\r\n--i\r\n"
            b"Content-Type: text/plain\r\n\r\nbody\r\n--o--\r\n",
            b"Content-Type: multipart/mixed; boundary=o\r\n\r\n--o\r\n"
            b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64\r\n"
            b"Content-Disposition: attachment; filename=x.pdf\r\n\r\n!!!a\r\n--o--\r\n",
        ]
        for raw in cases:
            with self.subTest(raw=raw[:40]), self.assertRaises(attachments.MimeStructureError):
                attachments.inspect_mime_tree(raw)

    def test_embedded_messages_fail_closed_even_without_nested_attachments(self):
        for disposition in (b"inline", b"attachment"):
            raw = (b"Content-Type: multipart/mixed; boundary=x\r\n\r\n--x\r\n"
                   b"Content-Type: message/rfc822\r\nContent-Disposition: " + disposition +
                   b"\r\n\r\nFrom: a@example.test\r\n\r\nbody\r\n--x--\r\n")
            with self.subTest(disposition=disposition), self.assertRaises(attachments.MimeStructureError):
                attachments.inspect_mime_tree(raw)

    def test_undecodable_leaf_cannot_produce_inventory(self):
        raw = attachments.build_test_eml()
        with patch("email.message.EmailMessage.get_payload", return_value=None):
            with self.assertRaises(attachments.MimeStructureError):
                attachments.inspect_mime_tree(raw)

    def test_unknown_transfer_encoding_is_not_treated_as_decoded(self):
        raw = (b"Content-Type: application/pdf\r\n"
               b"Content-Transfer-Encoding: x-unknown\r\n"
               b"Content-Disposition: attachment; filename=x.pdf\r\n\r\nQUJD")
        with self.assertRaises(attachments.MimeStructureError):
            attachments.inspect_mime_tree(raw)

    def test_valid_transfer_encoding_with_comment_is_decoded(self):
        for encoding, payload in ((b"base64", b"QUJD"), (b"quoted-printable", b"=41BC")):
            raw = (b"Content-Type: application/pdf\r\n"
                   b"Content-Transfer-Encoding: " + encoding + b" (comment)\r\n"
                   b"Content-Disposition: attachment; filename=x.pdf\r\n\r\n" + payload)
            with self.subTest(encoding=encoding):
                result = attachments.inspect_mime_tree(raw)
                self.assertEqual(result[0]["size_bytes"], 3)
                self.assertEqual(result[0]["sha256"],
                                 "b5d4045c3f466fa91fe2cc6abe79232a1a57cdf104f7a26e716e0a1e2789df78")

    def test_mime_error_is_item_local_and_forces_review(self):
        item = {"envelope_id": "1", "message_id": "id@example.test",
                "source_folder": "INBOX", "decision": {}}
        source = {"1": {"envelope_id": "1", "folder": "INBOX"}}
        def evaluate(**kwargs):
            return attachments.inspect_mime_tree(kwargs["raw_eml"])
        raw = b"Content-Type: message/rfc822\r\n\r\nFrom: x@example.test\r\n\r\nbody"
        result, handoff = reclassification._evaluate_item(
            item, source, evaluate_attachments=True, evaluate=evaluate,
            reclassify=lambda **kwargs: self.fail("unexpected reclassification"),
            read_raw_mime=lambda *args, **kwargs: raw,
            triggers_evaluation=lambda decision: True, rules_fingerprint="x",
            workspace_root=Path("."), data_dir=Path("data"), account="TEST",
            policy=None, run_id=None, lease_id=None, conversation_id=None,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "handoff_invalid")
        self.assertIsNone(handoff)
        self.assertTrue(item["decision"]["review_required"])
        self.assertEqual(item["action"]["target_folder"], "INBOX")


if __name__ == "__main__":
    unittest.main()
