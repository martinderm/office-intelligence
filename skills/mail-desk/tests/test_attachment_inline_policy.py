"""FR-17 / MD-R4 tests: inline-image policy for the automatic attachment evaluation.

These behavior tests define the MD-R4 target semantics at the public
``attachment_evaluate`` seam and the MD-A1 MIME-classification seam:

1. Inline signature parts (``content_disposition: inline`` with a concrete
   ``content_id``) and image MIME types without extractable text are **not**
   ``required_for_decision`` triggers of the automatic evaluation.  They stay inventory
   metadata and are fetched only through the existing human MD-A2 path (B-5:
   ``35-years-signature.png`` and ``IMAGE.png`` were automatically fetched with
   ``chars: 0`` and produced no routing signal, only quarantine/PII/quota surface).
2. The rule is deliberately narrow: a real, text-bearing attachment still triggers the
   automatic evaluation and is still fetched and extracted.
3. ``chars: 0`` edge cases: an image part with zero extractable characters is excluded,
   and a zero-byte text part is already policy-rejected in the MD-A1 inventory, so it can
   never become an automatic-evaluation trigger.
4. The human MD-A2 fetch path is unchanged: a human-approved fetch of the inline signature
   image still succeeds.

Interpretation of the narrow rule, recorded because the FR bundles two exclusion classes
in one sentence: a part is excluded from the automatic ``required_for_decision`` trigger
when it is an image MIME type without extractable routing text, which covers both the
inline signature image and a non-inline ``image/png`` attachment.  The inline clause alone
does not exclude a text-bearing non-image part: an inline ``text/plain`` part that carries
extractable text still triggers, because it has extractable text and is not an image MIME
type.

The module is tests-only: no production, documentation or system-map file is touched.
It is hermetic (no mailbox access, no credentials, no network), deterministic and uses the
standard library plus the workspace's own modules.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import attachment_evaluation as aevaluate  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
from core import attachments  # noqa: E402
from core.attachment_policy import DEFAULT_ATTACHMENT_POLICY  # noqa: E402
import mime_timeout_fixtures as fixtures  # noqa: E402


ACCOUNT = "mdr4-inline-policy"
FOLDER = "INBOX"
ENVELOPE_ID = "9400"
LEASE = "lease-mdr4-inline"
CONV = "conv-mdr4-inline"
HUMAN_RUN_ID = "run_mdr4human"


class _InlinePolicyHarness:
    """Shared hermetic harness: an owned workspace lock plus an isolated preflight."""

    def setUp(self) -> None:
        self.maxDiff = None
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace_root = self._tmp.name
        guard = afetch._load_workspace_lock_guard()
        guard.acquire_workspace_lock(
            self.workspace_root,
            harness="mdr4-inline-policy-test",
            lease_id=LEASE,
            conversation_id=CONV,
        )
        self._preflight_patcher = patch.object(
            afetch, "verify_no_tracked_quarantine", return_value=None, create=True
        )
        self._preflight_patcher.start()
        self.addCleanup(self._preflight_patcher.stop)

    def _control_plane(self) -> dict[str, Any]:
        """Trusted fetch/extract control-plane bindings for the real evaluation path."""
        return {
            "workspace_root": self.workspace_root,
            "data_dir": Path(self.workspace_root) / "data" / "mail-desk",
            "lease_id": LEASE,
            "conversation_id": CONV,
        }

    def _ambiguous_decision(self) -> dict[str, Any]:
        """The classifier's canonical ambiguous fallback decision (FR-15 trigger)."""
        return {
            "kind": "unknown",
            "id": "unclassified",
            "confidence": "low",
            "review_required": True,
        }

    def _evaluate(self, raw_eml: bytes, *, message_id: str) -> dict[str, Any]:
        return aevaluate.attachment_evaluate(
            raw_eml=raw_eml,
            account=ACCOUNT,
            folder=FOLDER,
            envelope_id=ENVELOPE_ID,
            message_id=message_id,
            decision=self._ambiguous_decision(),
            **self._control_plane(),
        )

    def _recording_fetch(self, calls: list[dict[str, Any]]) -> Any:
        """Wrap the canonical fetch so the automatic fetch attempts are observable."""
        real_fetch = aevaluate.op_attachment_fetch

        def _wrapped(**kwargs: Any) -> dict[str, Any]:
            calls.append(dict(kwargs))
            return real_fetch(**kwargs)

        return _wrapped

    def _unavailable_extraction(self) -> Any:
        """Return a fast canonical extraction with no extractable text (chars 0)."""

        def _extract(fetch_result: Any, expected_sha256: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "status": "attachment_conversion_unavailable",
                "quality": "low",
                "truncation_reason": None,
                "character_count": 0,
                "source_character_count": None,
                "text": "",
                "error": "No extraction converter available in the isolated MD-R4 test.",
            }

        return _extract

    def _filenames(self, staged: dict[str, Any]) -> list[str]:
        return [str(entry.get("filename", "")) for entry in staged.get("files", [])]


class InlineImagePolicyRedTests(_InlinePolicyHarness, unittest.TestCase):
    """B-5: image parts without extractable text must not trigger an automatic fetch."""

    def test_inline_signature_image_is_not_automatically_fetched(self) -> None:
        """An inline signature PNG today causes an automatic fetch; target: none."""
        raw_eml = fixtures.inline_signature_eml()
        calls: list[dict[str, Any]] = []
        with patch.object(
            aevaluate, "op_attachment_fetch", side_effect=self._recording_fetch(calls)
        ) as mocked_fetch, patch.object(
            aevaluate, "extract_attachment_content", side_effect=self._unavailable_extraction()
        ):
            result = self._evaluate(
                raw_eml, message_id=fixtures.INLINE_SIGNATURE_MESSAGE_ID
            )

        staged = result["attachment_evaluation"]
        self.assertEqual(
            0,
            mocked_fetch.call_count,
            "inline signature images must not be fetched by the automatic evaluation",
        )
        self.assertNotIn(fixtures.INLINE_SIGNATURE_FILENAME, self._filenames(staged))
        self.assertEqual([], calls)

    def test_non_inline_image_without_extractable_text_is_not_automatically_fetched(self) -> None:
        """A real attached PNG carries no extractable text and must not be auto-fetched."""
        raw_eml = fixtures.non_inline_image_eml()
        with patch.object(
            aevaluate, "op_attachment_fetch", side_effect=self._recording_fetch([])
        ) as mocked_fetch, patch.object(
            aevaluate, "extract_attachment_content", side_effect=self._unavailable_extraction()
        ):
            result = self._evaluate(raw_eml, message_id=fixtures.NON_INLINE_IMAGE_MESSAGE_ID)

        staged = result["attachment_evaluation"]
        self.assertEqual(
            0,
            mocked_fetch.call_count,
            "image MIME types without extractable text must not be fetched automatically",
        )
        self.assertNotIn(fixtures.NON_INLINE_IMAGE_FILENAME, self._filenames(staged))


class RoutingRelevantAttachmentTests(_InlinePolicyHarness, unittest.TestCase):
    """Characterization: the narrow rule never excludes text-bearing attachments."""

    def test_text_bearing_attachment_still_triggers_and_is_fetched(self) -> None:
        """An attachment with extractable text is still fetched and handed off."""
        raw_eml = fixtures.routing_attachment_eml()
        with patch.object(
            aevaluate, "op_attachment_fetch", side_effect=self._recording_fetch([])
        ) as mocked_fetch:
            result = self._evaluate(raw_eml, message_id=fixtures.ROUTING_ATTACHMENT_MESSAGE_ID)

        staged = result["attachment_evaluation"]
        self.assertEqual(1, mocked_fetch.call_count)
        self.assertEqual("completed", staged["status"])
        self.assertEqual("handoff_ready", staged["reason"])
        self.assertEqual([fixtures.ROUTING_ATTACHMENT_FILENAME], self._filenames(staged))
        self.assertEqual(
            len(fixtures.ROUTING_ATTACHMENT_TEXT),
            staged["files"][0]["chars"],
            "the extractable attachment text must reach the staged files metadata",
        )

    def test_inline_text_part_with_extractable_text_is_not_excluded_by_the_inline_clause(self) -> None:
        """An inline, text-bearing non-image part still triggers the automatic evaluation."""
        raw_eml = fixtures.inline_text_eml()
        with patch.object(
            aevaluate, "op_attachment_fetch", side_effect=self._recording_fetch([])
        ) as mocked_fetch:
            result = self._evaluate(raw_eml, message_id=fixtures.INLINE_TEXT_MESSAGE_ID)

        staged = result["attachment_evaluation"]
        self.assertEqual(1, mocked_fetch.call_count)
        self.assertEqual("completed", staged["status"])
        self.assertEqual([fixtures.INLINE_TEXT_FILENAME], self._filenames(staged))


class ZeroCharacterEdgeCaseTests(_InlinePolicyHarness, unittest.TestCase):
    """``chars: 0`` edge cases are excluded from the automatic trigger."""

    def test_inline_signature_image_zero_char_extraction_is_excluded(self) -> None:
        """The B-5 image yields chars 0; after the fix it produces no ``files[]`` entry."""
        raw_eml = fixtures.inline_signature_eml()
        with patch.object(
            aevaluate, "op_attachment_fetch", side_effect=self._recording_fetch([])
        ) as mocked_fetch, patch.object(
            aevaluate, "extract_attachment_content", side_effect=self._unavailable_extraction()
        ):
            result = self._evaluate(
                raw_eml, message_id=fixtures.INLINE_SIGNATURE_MESSAGE_ID
            )

        staged = result["attachment_evaluation"]
        self.assertEqual(0, mocked_fetch.call_count)
        self.assertFalse(
            any(entry.get("chars") == 0 for entry in staged.get("files", [])),
            "no fetched inline image with zero extractable characters may remain",
        )

    def test_zero_byte_text_attachment_is_policy_rejected_and_never_a_trigger(self) -> None:
        """A zero-byte text part has no extractable text and is rejected by the inventory."""
        parts = attachments.inspect_mime_tree(fixtures.zero_byte_attachment_eml())
        empty_part = next(
            part for part in parts if part["filename"] == fixtures.EMPTY_ATTACHMENT_FILENAME
        )

        self.assertNotEqual("allowed", empty_part["policy_status"])
        self.assertFalse(empty_part["is_inline"])
        self.assertEqual(0, empty_part["size_bytes"])


class HumanApprovalPathCharacterizationTests(_InlinePolicyHarness, unittest.TestCase):
    """The human MD-A2 path is untouched by the automatic-evaluation policy."""

    def test_human_approved_fetch_of_inline_signature_image_still_works(self) -> None:
        """A human-approved fetch of the inline PNG still fetches it into quarantine."""
        raw_eml = fixtures.inline_signature_eml()
        message_id = fixtures.INLINE_SIGNATURE_MESSAGE_ID
        parts = attachments.inspect_mime_tree(raw_eml)
        png_part = next(
            part for part in parts if part["filename"] == fixtures.INLINE_SIGNATURE_FILENAME
        )
        candidate = attachments.bind_attachment_candidate(
            png_part, ACCOUNT, FOLDER, ENVELOPE_ID, message_id
        )
        review_hash = afetch.compute_review_hash(
            account=ACCOUNT,
            message_id=message_id,
            folder=FOLDER,
            envelope_id=ENVELOPE_ID,
            part_locator=png_part["part_locator"],
            inventory_sha256=png_part["sha256"],
        )
        receipt = {
            "receipt_id": "mdr4-human-approval",
            "request_hash": review_hash,
            "approved_at": "2026-09-22T09:00:00Z",
            "approved_by": "human-reviewer",
        }
        control = self._control_plane()

        result = afetch.op_attachment_fetch(
            candidate=candidate,
            account=ACCOUNT,
            folder=FOLDER,
            envelope_id=ENVELOPE_ID,
            message_id=message_id,
            part_locator=png_part["part_locator"],
            inventory_sha256=png_part["sha256"],
            review_hash=review_hash,
            approval_receipt=receipt,
            run_id=HUMAN_RUN_ID,
            raw_eml=raw_eml,
            data_dir=control["data_dir"],
            workspace_root=control["workspace_root"],
            lease_id=LEASE,
            conversation_id=CONV,
        )

        self.assertIn(result["status"], {"fetched", "already_fetched"})
        self.assertEqual(fixtures.INLINE_SIGNATURE_FILENAME, result["filename"])
        self.assertEqual(png_part["sha256"], result["fetch_sha256"])

    def test_md_a1_mime_policy_still_allows_images(self) -> None:
        """The MD-R4 policy is scoped to the automatic trigger, not the MD-A1 allowlist."""
        allowed = DEFAULT_ATTACHMENT_POLICY["transport"]["allowed_mime_types"]
        self.assertIn("image/png", allowed)
        self.assertIn("image/jpeg", allowed)


if __name__ == "__main__":
    unittest.main()
