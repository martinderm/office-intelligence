"""FR-20/MD-A3 inline-quota tests.

Hermetic contract tests. They exercise the real attachment inventory/policy
against a deterministic MIME message (6 inline images before 2 .docx files,
mirroring the Env 9438 finding); no mailbox, network or catalog access.

Red-Gate history: at the MD-A3-001 dispatch inline parts consumed the file
attachment count quota (``max_attachments_per_message``), so inline 4-6
pushed the real .docx files to ``skipped_count_limit`` and no separate inline
limit existed; the tests below failed then and pin the resolved three-state
contract since.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.attachments import build_test_eml, inspect_mime_tree  # noqa: E402
from core.quarantine import attachment_policy  # noqa: E402


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_DATA = (
    b"PK\x03\x04" + b"word/document.xml" + b"x" * 200
)


def _env_9438_eml() -> bytes:
    """Env 9438 shape: 6 inline signature images first, then 2 real .docx."""
    inline = [
        {"cid": f"sig{i}", "filename": f"signature{i}.png", "mime_type": "image/png", "data": b"\x89PNG\r\n\x1a\n" + b"i" * (20 + i)}
        for i in range(1, 7)
    ]
    return build_test_eml(
        subject="Wtrlt: FW: Reaching out to our partners.",
        body_html='<div>Body with ' + "".join(f'<img src="cid:sig{i}">' for i in range(1, 7)) + "</div>",
        inline_images=inline,
        attachments=[
            {"filename": "EVOLVE_Partner_Communication.docx", "mime_type": DOCX_MIME, "data": DOCX_DATA},
            {"filename": "PIN_Seconds_Malta.docx", "mime_type": DOCX_MIME, "data": DOCX_DATA + b"2"},
        ],
    )


class InlineQuotaPolicyTests(unittest.TestCase):
    """check_attachment_policy separates file and inline count quotas."""

    def test_policy_exposes_inline_limit_and_new_reason(self) -> None:
        self.assertIn(
            "max_inline_per_message",
            attachment_policy.DEFAULT_ATTACHMENT_POLICY["transport"],
            "default policy must carry the inline quota (FR-20/MD-A3)",
        )
        self.assertEqual(3, attachment_policy.DEFAULT_ATTACHMENT_POLICY["transport"]["max_inline_per_message"])

    def test_inline_part_beyond_inline_limit_gets_skipped_inline_limit(self) -> None:
        policy = dict(attachment_policy.DEFAULT_ATTACHMENT_POLICY)
        status, reason = attachment_policy.check_attachment_policy(
            filename="signature4.png",
            mime_type="image/png",
            size_bytes=30,
            current_index=3,
            cumulative_bytes=0,
            is_inline=True,
            inline_index=3,
            policy=policy,
        )
        self.assertEqual("skipped_inline_limit", status)
        self.assertIn("inline", reason.lower())

    def test_inline_within_limit_is_allowed_and_does_not_consume_file_quota(self) -> None:
        status, _ = attachment_policy.check_attachment_policy(
            filename="signature1.png",
            mime_type="image/png",
            size_bytes=30,
            current_index=0,
            inline_index=0,
            cumulative_bytes=0,
            is_inline=True,
        )
        self.assertEqual("allowed", status)


class Env9438InventoryTests(unittest.TestCase):
    """The observed Env 9438 shape resolves into the three-state contract."""

    def test_six_inline_images_plus_two_docx_three_states_per_class(self) -> None:
        inventory = inspect_mime_tree(_env_9438_eml())
        inline_entries = [entry for entry in inventory if entry.get("is_inline")]
        file_entries = [entry for entry in inventory if not entry.get("is_inline")]

        self.assertEqual(6, len(inline_entries), f"6 inline images expected, got {len(inline_entries)}")
        self.assertEqual(2, len(file_entries), f"2 docx files expected, got {len(file_entries)}")

        allowed_inline = [entry for entry in inline_entries if entry["policy_status"] == "allowed"]
        over_limit_inline = [entry for entry in inline_entries if entry["policy_status"] == "skipped_inline_limit"]
        self.assertEqual(3, len(allowed_inline), "first 3 inline images allowed up to the inline limit")
        self.assertEqual(3, len(over_limit_inline), "inline images 4-6 exceed the inline limit")
        self.assertNotIn(
            "skipped_count_limit",
            [entry["policy_status"] for entry in inline_entries],
            "inline parts must never carry skipped_count_limit (FR-20/MD-A3)",
        )

        for entry in file_entries:
            self.assertEqual("allowed", entry["policy_status"], f"real attachment must be policy-checked, got {entry['policy_status']}: {entry.get('policy_reason')}")

    def test_real_docx_files_are_not_pushed_to_count_limit(self) -> None:
        inventory = inspect_mime_tree(_env_9438_eml())
        statuses = {entry["filename"]: entry["policy_status"] for entry in inventory}
        self.assertEqual(
            {"EVOLVE_Partner_Communication.docx": "allowed", "PIN_Seconds_Malta.docx": "allowed"},
            {name: status for name, status in statuses.items() if name.endswith(".docx")},
        )
        self.assertNotIn(
            "skipped_count_limit",
            statuses.values(),
            "no part may carry skipped_count_limit in the Env 9438 shape (quota separation)",
        )


if __name__ == "__main__":
    unittest.main()